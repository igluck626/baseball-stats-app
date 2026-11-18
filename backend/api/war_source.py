"""Where the API's WAR figures come from (approved 2026-11-18).

One server setting, WAR_VERSION, picks the source for every WAR the API serves:

  unset / empty  the stored Baseball-Reference columns (player_seasons."WAR", "WAR_off", "WAR_def"; pitcher_seasons."WAR"),
                 exactly as before this module existed
  'v1'           our own WAR, from the war_seasons table rows of that method_version (computed by backend/war)

Flipping the setting switches every endpoint at once (player pages, careers, leader badges, leaderboards, award voting,
rosters, /ask); unsetting it switches back. Nothing is rewritten either way.

With our WAR, a season with no war_seasons row reads as:
  * a hidden league (Negro Leagues circuits): None ("—"), and left out of career totals
  * a hitting row with no plate appearances (a pitcher who never batted) or a pitching row with no innings: 0.0,
    nothing to rate, as the stored figures always did
  * anything else (a season with playing time the table doesn't hold yet, or one the calculation couldn't rate): None,
    unknown, and any career total over it is unknown too
Figures are served to one decimal, like the stored ones, rounded half away from zero in Python and in SQL alike (so a
leader badge's extreme equals the leader's own value). Hitting offense/defense are served in the WAR_off / WAR_def
fields; defense is WAR − offense after rounding, so the two shown parts always add up to the WAR shown. The
Baseball-Reference-only fields (WAA, runs_above_avg, runs_above_rep) are None under our WAR.
"""
import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import Float, Numeric, and_, case, cast, func

from database.models import WarSeason
from postseason_stats import NEGRO_CIRCUITS

HIDDEN_LEAGUES = frozenset(NEGRO_CIRCUITS)
BREF_ONLY = ("WAA", "runs_above_avg", "runs_above_rep")


def r1(x):
    """One decimal, half away from zero, from the float's shortest decimal form: the same result as Postgres'
    round(x::numeric, 1)."""
    return None if x is None else float(Decimal(repr(float(x))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def r1_sql(expr):
    return cast(func.round(cast(expr, Numeric), 1), Float)


def active_version() -> Optional[str]:
    """The war_seasons method_version the API serves, or None for the stored Baseball-Reference columns."""
    v = (os.environ.get("WAR_VERSION") or "").strip()
    return v or None


def role_of(table) -> str:
    return "pit" if table.__tablename__ == "pitcher_seasons" else "hit"


def player_rows(db, player_id: int, role: str) -> dict:
    """{year: (war, offense, defense)} for one player and role, from the active version."""
    rows = (db.query(WarSeason.year, WarSeason.war, WarSeason.offense, WarSeason.defense)
            .filter(WarSeason.player_id == player_id, WarSeason.role == role,
                    WarSeason.method_version == active_version())
            .all())
    return {y: (w, o, d) for y, w, o, d in rows}


def _no_playing_time(season: dict, role: str) -> bool:
    if role == "hit":
        return not (season.get("PA") or 0) and not (season.get("AB") or 0)
    return not (season.get("IP") or 0)


def apply_to_season(season: dict, found: Optional[tuple], role: str) -> None:
    """Overwrite a season dict's WAR fields with our figures (call only when active_version() is set)."""
    if found is not None:
        war, off = r1(found[0]), r1(found[1])
        dfn = None if (war is None or off is None) else round(war - off, 1)
    elif season.get("league") in HIDDEN_LEAGUES:
        war = off = dfn = None
    elif _no_playing_time(season, role):
        war, off, dfn = 0.0, (0.0 if role == "hit" else None), (0.0 if role == "hit" else None)
    else:
        war = off = dfn = None
    season["WAR"] = war
    if role == "hit":
        season["WAR_off"] = off
        season["WAR_def"] = dfn
    for k in BREF_ONLY:
        if k in season:
            season[k] = None


def apply_to_seasons(db, player_id: int, seasons: list, role: str) -> None:
    """apply_to_season for every season dict of one player (no-op when the stored columns are in use)."""
    if not active_version():
        return
    got = player_rows(db, player_id, role)
    for s in seasons:
        apply_to_season(s, got.get(s.get("year")), role)


def career_total(seasons: list, key: str) -> Optional[float]:
    """A career WAR figure under our WAR: hidden-league seasons are left out; any other season with no figure makes
    the total unknown (None), never a short sum."""
    shown = [s for s in seasons if s.get("league") not in HIDDEN_LEAGUES]
    if any(s.get(key) is None for s in shown):
        return None
    return round(sum(s.get(key) or 0.0 for s in shown), 1)


def join_on(table):
    """Join condition from a season table to its war_seasons row (same player, year, role, active version)."""
    return and_(WarSeason.player_id == table.player_id, WarSeason.year == table.year,
                WarSeason.role == role_of(table), WarSeason.method_version == active_version())


def season_value_sql(table):
    """SQL value of one season row's WAR under our WAR (for queries that OUTER JOIN war_seasons via join_on)."""
    if role_of(table) == "hit":
        empty = and_(func.coalesce(table.PA, 0) == 0, func.coalesce(table.AB, 0) == 0)
    else:
        empty = func.coalesce(table.IP, 0) == 0
    return case(
        (WarSeason.war.isnot(None), r1_sql(WarSeason.war)),
        (table.league.in_(HIDDEN_LEAGUES), None),
        (empty, 0.0),
        else_=None,
    )


def career_sum_sql(table):
    """SUM of WAR over a player's season rows under our WAR, NULL when any non-hidden season has no figure.
    Needs the same OUTER JOIN as season_value_sql."""
    v = season_value_sql(table)
    hidden = func.coalesce(table.league, "").in_(HIDDEN_LEAGUES)
    unknown = func.max(case((and_(~hidden, v.is_(None)), 1), else_=0))
    return case((unknown == 1, None), else_=func.sum(case((hidden, None), else_=v)))


def row_war(db, row, role: str):
    """WAR for one season-table ORM row: the stored column, or ours by the rules above when active."""
    if not active_version():
        return row.WAR
    s = {"year": row.year, "league": row.league, "PA": getattr(row, "PA", None), "AB": getattr(row, "AB", None),
         "IP": getattr(row, "IP", None)}
    apply_to_season(s, player_rows(db, row.player_id, role).get(row.year), role)
    return s["WAR"]


def ask_sql(table: str) -> tuple:
    """(value, unknown) SQL fragments for /ask's raw-SQL season sums over `table` ('player_seasons' or
    'pitcher_seasons', referenced unaliased): value = one season's WAR under our WAR, NULL for hidden leagues;
    unknown = TRUE when any non-hidden season has no figure. Bind :war_v = active_version()."""
    role = "pit" if table == "pitcher_seasons" else "hit"
    empty = (f'COALESCE({table}."IP", 0) = 0' if role == "pit"
             else f'COALESCE({table}."PA", 0) = 0 AND COALESCE({table}."AB", 0) = 0')
    hidden = "(" + ", ".join(f"'{x}'" for x in sorted(HIDDEN_LEAGUES)) + ")"
    sub = (f"(SELECT w.war FROM war_seasons w WHERE w.player_id = {table}.player_id AND w.year = {table}.year "
           f"AND w.role = '{role}' AND w.method_version = :war_v)")
    is_hidden = f"COALESCE({table}.league, '') IN {hidden}"
    value = f"(CASE WHEN {is_hidden} THEN NULL WHEN {sub} IS NOT NULL THEN round(({sub})::numeric, 1)::float8 WHEN {empty} THEN 0.0 ELSE NULL END)"
    unknown = f"BOOL_OR(NOT ({is_hidden}) AND {value} IS NULL)"
    return value, unknown
