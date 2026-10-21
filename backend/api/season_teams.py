"""Per-club rows for a player's traded seasons — the player page's "All teams"
row and the club rows under it (GET /players/{id}/season-teams).

THE RULE is the loader's (scripts/load_season_teams.py, Isaac 2026-10-16):
Retrosheet's per-team rows where they add up exactly to our season row, else
Lahman's if THEY do, else no breakdown. Finished seasons come from the stored
player_season_teams / pitcher_season_teams; the current season is checked
live, the same way, against the nightly stints.

⚠️ A CLUB VALUE IS SHOWN ONLY WHERE IT IS VERIFIED. Each stored row lists, in
`verified`, the columns recorded on both sides that add up; every other column
goes out as null and the app shows "—". A missing value is never a 0.

⚠️ NO BREAKDOWN STILL NAMES THE CLUBS. A traded season whose club rows don't
add up (or that the stints alone show was split) is sent with its clubs and
every stat null, so the page can say which teams without printing numbers that
don't sum.
"""
from __future__ import annotations

import collections

from sqlalchemy import text

import load_season_teams as rule


def _cols(cols):
    return ", ".join(c if c in ("doubles", "triples") else f'"{c}"' for c in cols)


def _out_value(col, v):
    """A stored value as the wire sends it: IP as decimal thirds, the rest as ints."""
    if v is None:
        return None
    return round(float(v), 3) if col == "IP" else int(v)


def seasons(db, player_id: int, role: str) -> list[dict]:
    """[{year, source, teams: [{team, stats}]}] for every traded season of
    `role` ('bat' | 'pit'), oldest first. `source` is 'retrosheet', 'lahman',
    'nightly' (the current season, from the nightly stints) or None (no
    breakdown — the clubs alone, every stat null)."""
    t = rule.TABLES[role]
    store = rule.STORE[role]
    out: dict[int, dict] = {}

    stored = db.execute(text(
        f"SELECT year, stint_order, team, source, verified, {_cols(store)} FROM {t['out']} "
        "WHERE player_id = :p ORDER BY year, stint_order"), {"p": player_id}).mappings().all()
    for r in stored:
        ok = set((r["verified"] or "").split("|"))
        season = out.setdefault(r["year"], {"year": r["year"], "source": r["source"], "teams": []})
        season["teams"].append({"team": r["team"],
                                "stats": {c: (_out_value(c, r[c]) if c in ok else None) for c in store}})

    stints = collections.defaultdict(list)
    for r in db.execute(text(
            f"SELECT year, team, stint_order, {_cols(store)} FROM {t['stints']} "
            "WHERE player_id = :p ORDER BY year, stint_order, team"), {"p": player_id}).mappings():
        stints[r["year"]].append(r)
    season_rows = {r["year"]: r for r in db.execute(text(
        f"SELECT year, {_cols(store)} FROM {t['season']} WHERE player_id = :p"), {"p": player_id}).mappings()}

    for year, rows in stints.items():
        teams = list(dict.fromkeys(r["team"] for r in rows))
        if len(teams) < 2 or year in out or year not in season_rows:
            continue
        season = rule._row(store, [season_rows[year][c] for c in store], role)
        if role == "bat" and not (season.get("AB") or season.get("PA")):
            continue                      # a pitcher's empty batting row: nothing to split
        if year > rule.LAST_LOADED_SEASON:
            clubs = []
            for r in rows:
                club = rule._row(store, [r[c] for c in store], role)
                club.update(_team=r["team"], _order=r["stint_order"])
                clubs.append(club)
            source, chosen = rule.choose(role, season, clubs, [])
            if source:
                ok = set(rule.verified(chosen, season, store))
                ordered = sorted(chosen, key=lambda c: (c["_order"] or 0, c["_team"]))
                out[year] = {"year": year, "source": "nightly", "teams": [
                    {"team": c["_team"], "stats": {
                        col: ((round(c[col] / 3.0, 3) if col == "IP" else c[col]) if col in ok and c[col] is not None
                              else None) for col in store}}
                    for c in ordered]}
                continue
        out[year] = {"year": year, "source": None,
                     "teams": [{"team": tm, "stats": {c: None for c in store}} for tm in teams]}
    return [out[y] for y in sorted(out)]
