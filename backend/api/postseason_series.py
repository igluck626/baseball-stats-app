"""Postseason series state, derived from balldontlie's games and standings.

balldontlie lists every postseason game (`postseason: true`) but carries no
series, round, game-number or seed field. The series is ours to derive:

  • A SERIES is every postseason game between one unordered pair of teams —
    in the current format two teams meet at most once per postseason.
  • GAME NUMBERS follow start time. Postponed and cancelled games are left
    out; an if-necessary game balldontlie has not listed yet is NOT invented.
  • balldontlie PRE-LISTS games whose teams are not known yet as "UNK"
    (team id -1) — both sides for a later round, one side for a Division
    Series waiting on its Wild Card. Those are not a series yet and are
    skipped until balldontlie fills the team in.
  • It also lists IF-NECESSARY games (a Wild Card Game 3) before they are
    needed. They carry `if_necessary` until the series state guarantees them,
    and once a series is decided its unplayed games are dropped.
  • WINS are counted from finals only.
  • The ROUND follows from the two teams' playoff seeds (`/standings`
    `playoff_seed`), current format (2022 on):
        {3,6}, {4,5}                 -> Wild Card   (best of 3)
        {1,4}, {1,5}, {2,3}, {2,6}   -> Division    (best of 5)
        any other same-league pair   -> LCS         (best of 7)
        teams from different leagues -> World Series (best of 7)
    Validated against Lahman `series_post` for 2022-2025: 44 of 44 series,
    round and W-L both.

⚠️ SEED SAFETY. If a league's first-round pairs are not exactly {3,6} and
{4,5} — a seed is missing, or balldontlie's seeding disagrees with the games
actually scheduled — that league's rounds are NOT labelled: the series score
still shows, but with no round, no best-of, and therefore no "wins" or clinch
claims, which need the best-of to be true.

`league` is read from each team's own `league` field ("American" /
"National"). balldontlie's `conference_play` looks like the same signal and is
not: it is false for every 2025 postseason game, same-league ones included.

Pure functions only — the endpoint does the fetching.
"""
from __future__ import annotations

import datetime
from typing import Optional

# Seed corrections by season, applied over balldontlie's `playoff_seed`:
# {season: {team_abbreviation: seed}}. EMPTY on purpose — it exists so a wrong
# seed can be fixed by hand without a code change to the derivation.
SEED_OVERRIDES: dict[int, dict[str, int]] = {}

BEST_OF = {"WC": 3, "DS": 5, "CS": 7, "WS": 7}

_EXCLUDED_STATUSES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"}


def _known(team: dict) -> bool:
    """A real team, not balldontlie's "UNK" placeholder (id -1)."""
    return bool(team) and (team.get("id") or 0) > 0 and team.get("abbreviation") not in (None, "", "UNK")
_FIRST_ROUND = [frozenset({3, 6}), frozenset({4, 5})]
_DIVISION = [frozenset(s) for s in ({1, 4}, {1, 5}, {2, 3}, {2, 6})]
_LEAGUE_SHORT = {"American": "AL", "National": "NL"}


def league_of(team: dict) -> Optional[str]:
    """'AL' / 'NL' from a balldontlie team object, or None."""
    return _LEAGUE_SHORT.get((team or {}).get("league") or "")


def seeds_from_standings(standings: list[dict], season: int) -> dict[str, int]:
    """{abbreviation: seed} for the playoff teams (seeds 1-6 only — 7 and up
    is balldontlie's ordering of the teams that missed), overrides applied."""
    out: dict[str, int] = {}
    for row in standings:
        seed = row.get("playoff_seed")
        abbr = ((row.get("team") or {}).get("abbreviation"))
        if abbr and isinstance(seed, int) and 1 <= seed <= 6:
            out[abbr] = seed
    out.update(SEED_OVERRIDES.get(season, {}))
    return out


def classify_round(seed_a: Optional[int], seed_b: Optional[int],
                   league_a: Optional[str], league_b: Optional[str]) -> Optional[str]:
    """'WC' / 'DS' / 'CS' / 'WS', or None when it cannot be told."""
    if league_a is None or league_b is None:
        return None
    if league_a != league_b:
        return "WS"
    if seed_a is None or seed_b is None:
        return None
    pair = frozenset({seed_a, seed_b})
    if pair in _FIRST_ROUND:
        return "WC"
    if pair in _DIVISION:
        return "DS"
    return "CS"


def round_name(round_code: Optional[str], league: Optional[str]) -> Optional[str]:
    if round_code == "WS":
        return "World Series"
    if round_code is None or league is None:
        return None
    return {"WC": f"{league} Wild Card", "DS": f"{league}DS", "CS": f"{league}CS"}[round_code]


def _winner(game: dict) -> Optional[str]:
    if game.get("status") != "STATUS_FINAL":
        return None
    h = (game.get("home_team_data") or {}).get("runs")
    a = (game.get("away_team_data") or {}).get("runs")
    if h is None or a is None or h == a:
        return None
    return (game["home_team"] if h > a else game["away_team"])["abbreviation"]


def series_text(wins: dict[str, int], best_of: Optional[int]) -> Optional[str]:
    """'BOS leads 1-0' / 'Series tied 1-1' / 'BOS wins 2-1'; None before any
    final. 'wins' only when the best-of is known — without it a 2-0 lead is
    not a clinch we can vouch for."""
    (t1, w1), (t2, w2) = sorted(wins.items(), key=lambda kv: (-kv[1], kv[0]))
    if w1 == 0 and w2 == 0:
        return None
    if w1 == w2:
        return f"Series tied {w1}-{w2}"
    if best_of is not None and w1 >= best_of // 2 + 1:
        return f"{t1} wins {w1}-{w2}"
    return f"{t1} leads {w1}-{w2}"


def _round_labels_trusted(first_game: dict[frozenset, str], seeds: dict[str, int],
                          leagues: dict[str, Optional[str]]) -> dict[str, bool]:
    """{league: trusted}. `first_game` maps each series (a team pair) to its
    first game's start. A league's labels are trusted only if every team in
    its series has a seed, and every seed-3-to-6 team's FIRST series is
    against its first-round partner (3 v 6, 4 v 5) — i.e. the first-round
    pairs are exactly {3,6} and {4,5}."""
    trusted = {"AL": True, "NL": True}
    for pair in first_game:
        a, b = sorted(pair)
        lg = leagues.get(a)
        if lg is None or lg != leagues.get(b):
            continue                      # cross-league: judged via both leagues
        if seeds.get(a) is None or seeds.get(b) is None:
            trusted[lg] = False
    for team, seed in seeds.items():
        if not 3 <= seed <= 6:
            continue
        mine = [p for p in first_game if team in p]
        if not mine:
            continue                      # not scheduled yet: nothing to contradict
        earliest = min(mine, key=lambda p: first_game[p])
        partner = next(iter(earliest - {team}))
        if seeds.get(partner) != 9 - seed:
            trusted[leagues.get(team) or ""] = False
    return trusted


def build_series(games: list[dict], standings: list[dict], season: int) -> list[dict]:
    """Every postseason series in `games`, earliest first. See the module note."""
    seeds = seeds_from_standings(standings, season)
    by_pair: dict[frozenset, list[dict]] = {}
    teams: dict[str, dict] = {}
    for g in games:
        if not g.get("postseason") or g.get("status") in _EXCLUDED_STATUSES:
            continue
        h, a = g["home_team"], g["away_team"]
        if not (_known(h) and _known(a)) or h["abbreviation"] == a["abbreviation"]:
            continue
        teams[h["abbreviation"]] = h
        teams[a["abbreviation"]] = a
        by_pair.setdefault(frozenset({h["abbreviation"], a["abbreviation"]}), []).append(g)

    leagues = {abbr: league_of(t) for abbr, t in teams.items()}
    first_game = {p: min((g.get("date") or "") for g in gs) for p, gs in by_pair.items()}
    trusted = _round_labels_trusted(first_game, seeds, leagues)

    out: list[dict] = []
    for pair, gs in by_pair.items():
        gs.sort(key=lambda g: (g.get("date") or "", g.get("id") or 0))
        t1, t2 = sorted(pair)
        lg1, lg2 = leagues.get(t1), leagues.get(t2)
        rnd = classify_round(seeds.get(t1), seeds.get(t2), lg1, lg2)
        if rnd != "WS" and not trusted.get(lg1 or "", False):
            rnd = None
        if rnd == "WS" and not (trusted.get("AL", False) and trusted.get("NL", False)):
            rnd = None
        best_of = BEST_OF.get(rnd) if rnd else None
        name = round_name(rnd, lg1 if rnd != "WS" else None)
        need = best_of // 2 + 1 if best_of else None

        # The series as it stands now, from every final — decides whether a
        # listed-but-unplayed game is still only "if necessary", or moot.
        now = {t1: 0, t2: 0}
        for g in gs:
            w = _winner(g)
            if w:
                now[w] += 1
        played = sum(now.values())
        decided = bool(need) and max(now.values()) >= need
        if decided:
            gs = [g for g in gs if g.get("status") == "STATUS_FINAL"
                  or g.get("status") in ("STATUS_IN_PROGRESS", "STATUS_DELAYED")]

        wins = {t1: 0, t2: 0}
        game_rows = []
        for n, g in enumerate(gs, start=1):
            entering = dict(wins)
            winner = _winner(g)
            if winner:
                wins[winner] += 1
            final = g.get("status") == "STATUS_FINAL"
            # Guaranteed only while the leader cannot have clinched before it:
            # the leader needs (need - lead) more wins after `played` games.
            if_necessary = (not final and need is not None
                            and n > played + (need - max(now.values())))
            # Clinch / elimination are about the game about to be (or being)
            # played, so they read the state ENTERING it, and only mean
            # anything when the best-of is known.
            #
            # ⚠️ NOT FOR AN IF-NECESSARY GAME. Its entering state is not today's:
            # a Wild Card Game 3 is only played at 1-1, so reading tonight's 1-0
            # onto it ("ATL leads 1-0", ATL can clinch) describes a game that, if
            # it happens, will not look like that. It shows its label alone, with
            # no series state and no flags, until the series makes it certain.
            if if_necessary:
                clinch, elimination = [], []
            else:
                clinch = [t for t, w in entering.items() if need and w == need - 1] \
                    if not final and need and max(entering.values()) < need else []
                elimination = [t for t in (t1, t2) if any(o != t for o in clinch)] if clinch else []
            label = f"{name} · Game {n}" if name else f"Game {n}"
            if final:
                line = series_text(wins, best_of)
                status_text = line
            elif if_necessary:
                line = f"{label} (if necessary)"
                status_text = None
            else:
                status_text = series_text(entering, best_of)
                line = f"{label} · {status_text}" if status_text else label
            game_rows.append({
                "game_id": g.get("id"),
                "game_number": n,
                "date": g.get("date"),
                "status": g.get("status"),
                "home": g["home_team"]["abbreviation"],
                "away": g["away_team"]["abbreviation"],
                "home_runs": (g.get("home_team_data") or {}).get("runs"),
                "away_runs": (g.get("away_team_data") or {}).get("runs"),
                "label": label,
                "if_necessary": if_necessary,
                "series_status": status_text,
                "line": line,
                "can_clinch": bool(clinch),
                "clinch_teams": clinch,
                "elimination_game": bool(elimination),
                "elimination_teams": elimination,
            })

        over = bool(need) and max(wins.values()) >= need
        out.append({
            "teams": [t1, t2],
            "seeds": {t1: seeds.get(t1), t2: seeds.get(t2)},
            "league": lg1 if lg1 == lg2 else None,
            "round": rnd,
            "round_name": name,
            "best_of": best_of,
            "wins": wins,
            "is_over": over,
            "winner": max(wins, key=wins.get) if over else None,
            "games": game_rows,
        })
    out.sort(key=lambda s: (s["games"][0]["date"] or "", s["teams"]))
    return out
