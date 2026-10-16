"""The team page's reads for any club — its season's games and its roster —
assembled here so the app makes no balldontlie call of its own for them.

Pure functions only; `main.py` does the fetching and caching. Two endpoints:

  • GET /teams/{bdl_id}/schedule — the season's games for one team, in
    balldontlie's own shape (the app decodes them with the decoder it already
    has), plus whether the team's season is over, how it ended, and the next
    season's first game once balldontlie lists it. The hero card's last and
    next game, its strip of games and the Full Schedule sheet all read it.
  • GET /teams/{bdl_id}/roster — the active roster with each player's line
    FOR THIS TEAM, plus the injury list. See `roster_stats` for when a line is
    the team's own and when it is his whole season, labelled "all teams".
"""
from __future__ import annotations

import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import team_crosswalk

_EASTERN = ZoneInfo("America/New_York")

# balldontlie spells pitchers both ways (see PositionAbbreviation in the app).
_PITCHER_POSITIONS = {"P", "SP", "RP", "CL", "PITCHER", "STARTING PITCHER", "RELIEF PITCHER"}

# A game that will never be played is not "still to come".
_NOT_PENDING = {"STATUS_FINAL", "STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"}


def is_pitcher(position: Optional[str]) -> bool:
    return (position or "").strip().upper() in _PITCHER_POSITIONS


def retro_code(bdl_abbreviation: str) -> Optional[str]:
    """balldontlie's abbreviation -> the club's current Retrosheet code, the
    vocabulary of the stint tables ('LAD' -> 'LAN', 'LAA' -> 'ANA', 'OAK' ->
    'ATH'). The same route the nightly's stint writer takes, so the two agree."""
    fr = team_crosswalk.MODERN_CODE_TO_FRANCH.get(bdl_abbreviation)
    segs = team_crosswalk.FRANCH_RETRO_SEGMENTS.get(fr or bdl_abbreviation, ())
    return segs[-1][0] if segs else None


def _placeholder(game: dict) -> bool:
    """balldontlie pre-lists postseason games whose teams aren't known yet as
    team id -1 / "UNK" (BDLGame.hasPlaceholderTeam in the app)."""
    for side in ("home_team", "away_team"):
        t = game.get(side) or {}
        if (t.get("id") or 0) <= 0 or (t.get("abbreviation") or "").upper() == "UNK":
            return True
    return False


def schedule_games(games: list[dict]) -> list[dict]:
    """The season's games, placeholders dropped, deduplicated, in start order.
    Spring training stays: the hero's strip has always shown it in March, and
    the Full Schedule sheet filters to the regular season itself."""
    seen: set = set()
    out = []
    for g in games:
        if g.get("id") in seen or _placeholder(g):
            continue
        seen.add(g.get("id"))
        out.append(g)
    out.sort(key=lambda g: g.get("date") or "")
    return out


def eastern_date(iso: Optional[str]) -> Optional[datetime.date]:
    if not iso:
        return None
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(_EASTERN).date()


def regular_season_over(games: list[dict]) -> bool:
    """True once the team has no regular-season game still to be played. A
    postponed game that was never made up stays STATUS_POSTPONED for good
    (the Dodgers have one in 2026), so it does not hold the season open."""
    regular = [g for g in games if g.get("season_type") == "regular"]
    return bool(regular) and all(g.get("status") in _NOT_PENDING for g in regular)


def has_game_today(games: list[dict], today: datetime.date) -> bool:
    """A game on today's Eastern date that isn't over — the schedule is then
    cached for a minute rather than five."""
    return any(eastern_date(g.get("date")) == today and g.get("status") != "STATUS_FINAL"
               for g in games)


def season_result(bracket: Optional[dict], abbreviation: str, regular_over: bool) -> dict:
    """How the team's season stands, from the postseason bracket:

      {"state": "in_progress"}                     regular season still going
      {"state": "missed_postseason"}               not among the seeds
      {"state": "alive"}                           in the bracket, not out
      {"state": "eliminated", "round_name", "wins", "losses", "opponent"}
      {"state": "champion", "wins", "losses", "opponent"}

    A bracket whose league seeds aren't trusted, or no bracket at all, can't
    say a team missed the postseason, so that answers "season_over" — the
    regular season is done and nothing more is claimed."""
    if not regular_over:
        return {"state": "in_progress"}
    if not bracket:
        return {"state": "season_over"}
    slots = [s for lg in (bracket.get("leagues") or {}).values() for s in (lg.get("slots") or [])]
    ws = bracket.get("world_series")
    if ws:
        slots.append(ws)
    mine = [s for s in slots if abbreviation in [x.get("team") for x in (s.get("sides") or [])]]
    if not mine:
        leagues = (bracket.get("leagues") or {}).values()
        if leagues and all(lg.get("trusted") for lg in leagues):
            seeded = {x.get("team") for lg in leagues for x in (lg.get("seeds") or [])}
            if abbreviation not in seeded:
                return {"state": "missed_postseason"}
        return {"state": "season_over"}
    for s in mine:
        if s.get("state") != "complete" or not s.get("winner"):
            continue
        wins = ((s.get("series") or {}).get("wins") or {})
        opponent = next((x.get("team") for x in s.get("sides") or []
                         if x.get("team") and x.get("team") != abbreviation), None)
        line = {"round_name": s.get("round_name"), "wins": wins.get(abbreviation, 0),
                "losses": wins.get(opponent, 0), "opponent": opponent}
        if s.get("winner") != abbreviation:
            return {"state": "eliminated", **line}
        if s.get("round") == "WS":
            return {"state": "champion", **line}
    return {"state": "alive"}


def first_regular_game(games: list[dict]) -> Optional[dict]:
    """Next season's opener, once balldontlie lists it (it listed no 2027 game
    on 2026-10-15)."""
    regular = sorted((g for g in schedule_games(games) if g.get("season_type") == "regular"),
                     key=lambda g: g.get("date") or "")
    return regular[0] if regular else None


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

# The season counts a player's stints must add up to before his line for one
# team is trusted. Batting needs HBP and SF too, because OPS is built from them.
_BAT_CHECK = ("AB", "H", "doubles", "triples", "HR", "RBI", "BB", "HBP", "SF")
_PIT_CHECK = ("outs", "H", "ER", "BB", "SO", "W")


def outs(ip) -> int:
    """Decimal-thirds IP (6.333) -> outs."""
    return int(round(float(ip or 0) * 3))


def _counts(row: dict, pitcher: bool) -> dict:
    if pitcher:
        return {"outs": outs(row.get("IP")), **{c: int(row.get(c) or 0) for c in _PIT_CHECK if c != "outs"}}
    return {c: int(row.get(c) or 0) for c in _BAT_CHECK}


def _div(a, b):
    return a / b if b else None


def _team_batting(st: dict) -> dict:
    ab, h, bb = st.get("AB") or 0, st.get("H") or 0, st.get("BB") or 0
    hbp, sf = st.get("HBP") or 0, st.get("SF") or 0
    tb = st.get("TB") if st.get("TB") is not None else (
        h + (st.get("doubles") or 0) + 2 * (st.get("triples") or 0) + 3 * (st.get("HR") or 0))
    obp, slg = _div(h + bb + hbp, ab + bb + hbp + sf), _div(tb, ab)
    return {"g": st.get("G"), "avg": _div(h, ab), "hr": st.get("HR"), "rbi": st.get("RBI"),
            "ops": (obp + slg) if obp is not None and slg is not None else None,
            "era": None, "w": None, "so": None, "whip": None, "war": None}


def _team_pitching(st: dict) -> dict:
    o = outs(st.get("IP"))
    return {"g": st.get("G"), "avg": None, "hr": None, "rbi": None, "ops": None,
            "era": _div(27.0 * (st.get("ER") or 0), o), "w": st.get("W") or 0,
            "so": st.get("SO"), "whip": _div(3.0 * ((st.get("BB") or 0) + (st.get("H") or 0)), o),
            "war": None}


def _season_line(standard: dict, advanced: dict, pitcher: bool) -> dict:
    """The line the roster has always shown, from the season row."""
    if pitcher:
        return {"g": standard.get("G"), "avg": None, "hr": None, "rbi": None, "ops": None,
                "era": standard.get("ERA"), "w": standard.get("W") or 0, "so": standard.get("SO"),
                "whip": standard.get("WHIP"), "war": advanced.get("WAR")}
    return {"g": None, "avg": standard.get("BA"), "hr": standard.get("HR"), "rbi": standard.get("RBI"),
            "ops": standard.get("OPS"), "era": None, "w": None, "so": None, "whip": None,
            "war": advanced.get("WAR")}


def roster_stats(team: str, stints: list[dict], season: Optional[dict], pitcher: bool):
    """(scope, line) for one player on `team` (a Retrosheet code).

    His line is THIS TEAM's when it can be trusted, and his whole season,
    scope "all_teams", when it can't:

      • stints for this team only, and they add up to the season row: the
        season row IS this team's line — shown as it always was, WAR included.
      • stints for more than one team that add up to the season row: this
        team's stint, computed here. WAR is null: it is only stored per season,
        summed over teams (backfill_war.py), so it can't be split.
      • anything else — stints that don't add up, none for this team, none at
        all — is the season row, labelled "all_teams".

    `season` is {"standard", "advanced"} built from the season row (the
    current-stats payload's shape; WAR and the line come from the same row), or None;
    `stints` are this player's stint rows for the season, every team."""
    if not season:
        return None, None
    standard, advanced = season.get("standard") or {}, season.get("advanced") or {}
    mine = [s for s in stints if s.get("team") == team]
    total: dict = {}
    for s in stints:
        for k, v in _counts(s, pitcher).items():
            total[k] = total.get(k, 0) + v
    adds_up = bool(stints) and total == _counts(standard, pitcher)
    if mine and adds_up:
        if len({s.get("team") for s in stints}) == 1:
            return "team", _season_line(standard, advanced, pitcher)
        return "team", (_team_pitching if pitcher else _team_batting)(mine[0])
    return "all_teams", _season_line(standard, advanced, pitcher)
