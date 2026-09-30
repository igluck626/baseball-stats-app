"""Postseason player stats from our own game logs — the source rule, and the
pieces the /ask readers and /players/{id}/postseason share.

⚠️ ONE SOURCE PER SEASON, NEVER BOTH. `postseason_batting_gamelogs` /
`postseason_pitching_gamelogs` hold Retrosheet rows for the seasons Retrosheet
has published and balldontlie rows for the current one. When Retrosheet
publishes a season that balldontlie rows already cover, both sets exist for a
while; `postseason_source` picks exactly one, so nothing is counted twice:

    season <= retro_last  ->  Retrosheet
    season  > retro_last  ->  balldontlie

`retro_last` is read from the data (the newest season with Retrosheet rows),
so the switch happens by itself the day a season's Retrosheet rows are loaded.

Lahman's postseason tables are no longer read for player stats. They dropped
players whose ids carry an apostrophe or initials (Paul O'Neill, CC Sabathia,
J.D. Drew...) and filed some under a name-twin (Will / Willie Harris, Jacque /
Jason Jones). The known gap in the other direction: the Negro Leagues
postseason, which Lahman carries and Retrosheet does not.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import team_crosswalk

TABLE = {"bat": "postseason_batting_gamelogs", "pit": "postseason_pitching_gamelogs"}

# Columns each postseason table carries. A season-stats expression that reads
# anything else (CG, SHO, WAR...) can't be answered from these tables.
_COLUMNS = {
    "bat": {"PA", "AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB", "IBB", "SO", "SB", "CS",
            "HBP", "SF", "GIDP", "SH"},
    "pit": {"IP", "H", "R", "ER", "BB", "SO", "HR", "HBP", "W", "L", "SV", "GS"},
}

SOURCE_SQL = ("((season <= :ps_retro_last AND source = 'retrosheet') "
              "OR (season > :ps_retro_last AND source = 'bdl')) AND player_id IS NOT NULL")


def postseason_source(season: int, retro_last: int) -> str:
    """'retrosheet' for a season Retrosheet has published, else 'bdl'."""
    return "retrosheet" if season <= retro_last else "bdl"


_retro_last_cache: dict = {}


def retro_last(db) -> int:
    """The newest season with Retrosheet postseason rows, cached for an hour."""
    from sqlalchemy import text
    hit = _retro_last_cache.get("v")
    if hit and time.time() - hit[1] < 3600:
        return hit[0]
    v = db.execute(text("SELECT max(season) FROM postseason_batting_gamelogs "
                        "WHERE source = 'retrosheet'")).scalar() or 0
    _retro_last_cache["v"] = (int(v), time.time())
    return int(v)


def source_params(db) -> dict:
    return {"ps_retro_last": retro_last(db)}


def column_expr(role: str, season_expr: str) -> Optional[str]:
    """The season-stats column expression for a postseason table, or None
    when it reads a column the postseason tables don't have. A pitcher's G is
    one row per game, so it becomes a count."""
    if season_expr == '"G"':
        return "1" if role == "pit" else None
    names = set(re.findall(r'"([A-Za-z_]+)"|\b(doubles|triples)\b', season_expr))
    used = {a or b for a, b in names}
    return season_expr if used and used <= _COLUMNS[role] else None


def franchise_of(code: Optional[str], season: int) -> Optional[str]:
    """The franchise a team code means in `season`, through the team
    crosswalk, in three steps:
      1. the Retrosheet segment covering that season — each franchise's latest
         segment reaching forward, since Retrosheet stops before the current
         season;
      2. the modern-code table (balldontlie / Lahman 'LAA' -> the Angels);
      3. the franchise that used the code most recently (balldontlie still
         says 'OAK' for the Athletics, whose Retrosheet code became 'ATH').
    Retrosheet 'ANA' and balldontlie 'LAA' both land on the Angels this way,
    and 'LAA' in 1962 is still the Los Angeles Angels segment of that club."""
    if not code:
        return None
    latest = None
    for fr, segs in team_crosswalk.FRANCH_RETRO_SEGMENTS.items():
        last = max(s[2] for s in segs)
        for c, start, end in segs:
            if c != code:
                continue
            if start <= season and (season <= end or end == last):
                return fr
            if latest is None or end > latest[0]:
                latest = (end, fr)
    return team_crosswalk.MODERN_CODE_TO_FRANCH.get(code) or (latest[1] if latest else None)


# ---------------------------------------------------------------------------
# /players/{id}/postseason
# ---------------------------------------------------------------------------

_BAT_SUM = ("PA", "AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB", "IBB", "SO", "SB", "CS",
            "HBP", "SF", "GIDP", "SH")
_PIT_SUM = ("H", "R", "ER", "BB", "SO", "HR", "HBP", "W", "L", "SV", "GS")
ROUND_ORDER = {"WC": 0, "DS": 1, "CS": 2, "WS": 3}


def _rate(n, d):
    return round(n / d, 3) if d else None


def batting_rates(t: dict) -> dict:
    tb = t["H"] + t["doubles"] + 2 * t["triples"] + 3 * t["HR"]
    obp_d = t["AB"] + t["BB"] + t["HBP"] + t["SF"]
    avg, obp, slg = _rate(t["H"], t["AB"]), _rate(t["H"] + t["BB"] + t["HBP"], obp_d), _rate(tb, t["AB"])
    return {"AVG": avg, "OBP": obp, "SLG": slg,
            "OPS": round(obp + slg, 3) if obp is not None and slg is not None else None}


def pitching_rates(t: dict) -> dict:
    outs = t["outs"]
    return {"IP": f"{outs // 3}.{outs % 3}",
            "ERA": round(27 * t["ER"] / outs, 2) if outs else None,
            "WHIP": round(3 * (t["BB"] + t["H"]) / outs, 2) if outs else None}


def round_name(round_code: str, league: Optional[str]) -> str:
    if round_code == "WS":
        return "World Series"
    lg = league or ""
    return {"WC": f"{lg} Wild Card", "DS": f"{lg}DS", "CS": f"{lg}CS"}[round_code].strip()


def _team_info(db, pairs: set) -> dict:
    """{(code, season): {"franchise", "league", "name"}} through the crosswalk
    and team_seasons' franchise ids."""
    from sqlalchemy import text
    out = {}
    wanted = {(code, season, franchise_of(code, season)) for code, season in pairs}
    rows = {}
    frs = sorted({f for _, _, f in wanted if f})
    if frs:
        for fr, yr, lg, name in db.execute(text(
                "SELECT franch_id, year, league, team_name FROM team_seasons "
                "WHERE franch_id = ANY(:f) AND year = ANY(:y)"),
                {"f": frs, "y": sorted({s for _, s, _ in wanted})}):
            rows[(fr, yr)] = (lg, name)
    for code, season, fr in wanted:
        lg, name = rows.get((fr, season), (None, None))
        out[(code, season)] = {"franchise": fr, "league": lg, "name": name}
    return out


def player_postseason(db, player_id: int, current_season: int,
                      current_series: Optional[list] = None) -> dict:
    """Everything the profile's postseason view needs, per season, per round
    and career, from one source per season. `current_series` is
    `postseason_series.build_series` for `current_season` (None if unavailable)."""
    from sqlalchemy import text
    params = {"pid": player_id, **source_params(db)}
    sides = {}
    for kind, cols in (("bat", _BAT_SUM), ("pit", _PIT_SUM)):
        sel = ", ".join(f'"{c}"' if c not in ("doubles", "triples") else c for c in cols)
        extra = ', "IP"' if kind == "pit" else ""
        sides[kind] = [dict(r._mapping) for r in db.execute(text(
            f"SELECT season, round, team, opponent, game_id, source, {sel}{extra} FROM {TABLE[kind]} "
            f"WHERE player_id = :pid AND {SOURCE_SQL}"), params)]

    # Series results for every (season, round, team, opponent) the player saw,
    # from the team's game results in the same source.
    keys = {(r["season"], r["round"], r["team"], r["opponent"]) for rows in sides.values() for r in rows}
    results: dict = {}
    if keys:
        for season, rnd, team, opp, w, l in db.execute(text(
                "SELECT season, round, team, opponent, "
                "count(DISTINCT game_id) FILTER (WHERE result = 'W'), "
                "count(DISTINCT game_id) FILTER (WHERE result = 'L') "
                f"FROM {TABLE['bat']} WHERE {SOURCE_SQL.replace(' AND player_id IS NOT NULL', '')} "
                "AND season = ANY(:ss) AND team = ANY(:tt) GROUP BY 1, 2, 3, 4"),
                {**params, "ss": sorted({k[0] for k in keys}), "tt": sorted({k[2] for k in keys})}):
            results[(season, rnd, team, opp)] = (w, l)
    over = {}
    for s in current_series or []:
        over[frozenset(s["teams"])] = (s.get("is_over"), s.get("winner"))
    teams = _team_info(db, {(k[2], k[0]) for k in keys} | {(k[3], k[0]) for k in keys})

    def totals(rows, kind):
        cols = _BAT_SUM if kind == "bat" else _PIT_SUM
        t = {c: sum(r[c] or 0 for r in rows) for c in cols}
        t["G"] = len({r["game_id"] for r in rows})
        if kind == "pit":
            t["outs"] = sum(round((r["IP"] or 0) * 3) for r in rows)
            return {**t, **pitching_rates(t)}
        return {**t, **batting_rates(t)}

    def build(kind):
        rows = sides[kind]
        if not rows:
            return None
        seasons = []
        for season in sorted({r["season"] for r in rows}, reverse=True):
            srows = [r for r in rows if r["season"] == season]
            rounds = []
            for rnd, team, opp in sorted({(r["round"], r["team"], r["opponent"]) for r in srows},
                                         key=lambda k: ROUND_ORDER.get(k[0], 9)):
                rr = [r for r in srows if (r["round"], r["team"], r["opponent"]) == (rnd, team, opp)]
                w, l = results.get((season, rnd, team, opp), (0, 0))
                won: Optional[bool] = w > l
                if season == current_season:
                    fr = {franchise_of(team, season), franchise_of(opp, season)}
                    state = next((v for k, v in over.items()
                                  if {franchise_of(c, season) for c in k} == fr), None)
                    won = (state[1] is not None and franchise_of(state[1], season) == franchise_of(team, season)) \
                        if state and state[0] else None
                ti, oi = teams.get((team, season), {}), teams.get((opp, season), {})
                rounds.append({"round": rnd, "round_name": round_name(rnd, ti.get("league")),
                               "opponent": opp, "opponent_name": oi.get("name"),
                               "series": {"wins": w, "losses": l, "won": won},
                               "totals": totals(rr, kind)})
            team_codes = sorted({r["team"] for r in srows})
            seasons.append({"season": season, "source": srows[0]["source"],
                            "team": team_codes[0], "team_name": teams.get((team_codes[0], season), {}).get("name"),
                            "totals": totals(srows, kind), "rounds": rounds})
        return {"seasons": seasons, "career": totals(rows, kind)}

    cur_rows = [r for rows in sides.values() for r in rows if r["season"] == current_season]
    my_team = cur_rows[0]["team"] if cur_rows else None
    ws = next((s for s in current_series or [] if s.get("round") == "WS"), None)
    eliminated = None
    if my_team and current_series is not None:
        mine = franchise_of(my_team, current_season)
        eliminated = any(s.get("is_over") and s.get("winner")
                         and mine in {franchise_of(t, current_season) for t in s["teams"]}
                         and franchise_of(s["winner"], current_season) != mine
                         for s in current_series)
    return {
        "player_id": player_id,
        "retro_last": params["ps_retro_last"],
        "batting": build("bat"),
        "pitching": build("pit"),
        "current": {
            "season": current_season,
            # from the first postseason game until the World Series ends
            "league_in_progress": (bool(current_series) and not (ws and ws.get("is_over")))
                                  if current_series is not None else None,
            "player_appeared": bool(cur_rows),
            "team_eliminated": eliminated,
        },
    }


# ---------------------------------------------------------------------------
# Coverage: what the postseason logs can't answer, said out loud
# ---------------------------------------------------------------------------
#
# ⚠️ TWO KNOWN GAPS, NEVER SILENT. The logs start with the 1903 World Series
# (Retrosheet has no 1884-1890 championship series) and carry no Negro Leagues
# postseason. A question entirely inside a gap is DECLINED with the reason —
# never answered 0 or with partial data — and every career / range total and
# leaderboard states its coverage in the footnote (`game_coverage.note`, the
# same hook the play-by-play caveats use), so no prompt change is involved.

FIRST_SEASON = 1903
NEGRO_LEAGUES = {"NNL", "ECL", "ANL", "NSL", "EWL", "NN2", "NAL"}
# The Negro Leagues era's independent and regional circuits in the season tables
# (Buck Leonard's 1934 is 'IND'): no MLB postseason either.
NEGRO_CIRCUITS = NEGRO_LEAGUES | {"IND", "EAS", "WES", "NAC", "INT"}
# Only these leagues' seasons, from 1903, can have a postseason in the logs.
COVERED_LEAGUES = {"AL", "NL"}
_SPAN = "Postseason, 1903 to present"
_NEGRO = "the Negro Leagues postseason isn't included"
PRE_1903 = ("Postseason records before 1903 aren't available — they begin with the "
            "1903 World Series.")
NEGRO_DECLINE = "Negro Leagues postseason records aren't available."


def has_early_seasons(db, player_id: int, lo: Optional[int], hi: Optional[int]) -> bool:
    """Whether the player has a regular season before 1903 in [lo, hi]."""
    from sqlalchemy import text
    return bool(db.execute(text(
        "SELECT 1 FROM (SELECT year FROM player_seasons WHERE player_id = :p UNION "
        "SELECT year FROM pitcher_seasons WHERE player_id = :p) y "
        "WHERE year < :f AND (:lo IS NULL OR year >= :lo) AND (:hi IS NULL OR year <= :hi) LIMIT 1"),
        {"p": player_id, "f": FIRST_SEASON, "lo": lo, "hi": hi}).first())


def negro_league_seasons(db, player_id: int) -> set:
    """Seasons the player spent in a Negro league (regular-season tables)."""
    from sqlalchemy import text
    return {r[0] for r in db.execute(text(
        "SELECT year FROM player_seasons WHERE player_id = :p AND league = ANY(:l) UNION "
        "SELECT year FROM pitcher_seasons WHERE player_id = :p AND league = ANY(:l)"),
        {"p": player_id, "l": sorted(NEGRO_CIRCUITS)})}


def coverage(season=None, season_start=None, season_end=None,
             negro_seasons: Optional[set] = None, leaderboard: bool = False,
             early_seasons: bool = False) -> tuple:
    """(decline reason or None, footnote or None) for a postseason question.
    `negro_seasons`: the player's Negro Leagues seasons (None for a board);
    `early_seasons`: he has seasons before 1903 in the asked span."""
    lo = season if season is not None else season_start
    hi = season if season is not None else season_end
    if hi is not None and hi < FIRST_SEASON:
        return PRE_1903, None
    if season is not None and negro_seasons and season in negro_seasons:
        return NEGRO_DECLINE, None
    if season is not None:
        return None, None                        # one covered season: nothing to caveat
    note = _SPAN
    if lo is not None and lo < FIRST_SEASON:
        note += f"; {lo}-1902 isn't available"
    elif early_seasons:
        note += "; his seasons before 1903 aren't covered"
    if leaderboard or negro_seasons:
        note += f"; {_NEGRO}"
    return None, note + "."


def uncovered_career(db, player_id: int, lo: Optional[int], hi: Optional[int]) -> Optional[str]:
    """For a postseason total that found NO rows: the decline reason when every
    regular season the player has in [lo, hi] is outside the logs' coverage —
    before 1903, or in a Negro league — so '0' would be a gap, not an answer.
    None when any season in range is covered (then 0 is a real zero)."""
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT year, league FROM player_seasons WHERE player_id = :p UNION "
        "SELECT year, league FROM pitcher_seasons WHERE player_id = :p"), {"p": player_id}).fetchall()
    return uncovered_reason(rows, lo, hi)


def uncovered_reason(seasons, lo: Optional[int], hi: Optional[int]) -> Optional[str]:
    """The decision behind `uncovered_career`, on (year, league) pairs. Pure."""
    rows = [(y, lg) for y, lg in seasons if (lo is None or y >= lo) and (hi is None or y <= hi)]
    if not rows or any(lg in COVERED_LEAGUES and y >= FIRST_SEASON for y, lg in rows):
        return None                               # a covered season: 0 is a real zero
    if all(y < FIRST_SEASON for y, _ in rows):
        return PRE_1903
    if any(lg in NEGRO_CIRCUITS for _, lg in rows):
        return NEGRO_DECLINE
    return None
