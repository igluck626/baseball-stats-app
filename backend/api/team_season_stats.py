"""Team batting and pitching totals for a season, with MLB and league ranks — the
team page's Season Stats card.

SOURCE: balldontlie's team season totals (`/teams/season_stats`), Isaac's call
2026-10-16. They agree with balldontlie's standings on W, L and runs for all 30
clubs and with the sum of its own box lines on AB, HR and R; our per-team sums
run short wherever a game line is missing or a cell is unknown.

⚠️ THE ENDPOINT'S TRAPS, measured 2026-10-16:
  • `team_ids[]` is SILENTLY IGNORED — it returns all 60 rows. Never use it;
    `team_id` (singular) is the one that filters. We take every club in one
    `season=` call anyway.
  • `seasons[]` is rejected (400): it is `season=`.
  • 60 rows = each club's regular season AND postseason. Keep
    `season_type == "regular"` only.
  • Innings come in baseball notation ("1435.2" = 1435⅔).
  • No PA, HBP, SF, CS or IBB: OBP is taken as balldontlie ships it.

Pure functions; main.py fetches, caches and serves.
"""
from __future__ import annotations

from typing import Optional


# (key, label, side, lower_is_better)
STATS = [
    ("R", "Runs", "bat", False),
    ("AVG", "AVG", "bat", False),
    ("OBP", "OBP", "bat", False),
    ("SLG", "SLG", "bat", False),
    ("OPS", "OPS", "bat", False),
    ("HR", "Home runs", "bat", False),
    ("SB", "Stolen bases", "bat", False),
    ("ERA", "ERA", "pit", True),
    ("WHIP", "WHIP", "pit", True),
    ("SO", "Strikeouts", "pit", False),
    ("SV", "Saves", "pit", False),
    ("HRA", "Home runs allowed", "pit", True),
]


def outs_from_bdl_ip(v) -> Optional[int]:
    """balldontlie's baseball-notation innings ("1435.2") -> outs."""
    if v is None or v == "":
        return None
    whole, _, frac = str(v).partition(".")
    return int(whole or 0) * 3 + (int(frac[:1]) if frac[:1].isdigit() else 0)


def bdl_totals(rows: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """({abbreviation: {"bat": {...}, "pit": {...}}}, {abbreviation: team}) from
    balldontlie's /teams/season_stats rows — REGULAR SEASON ONLY. AVG, SLG, ERA
    and WHIP come from the counts (so equal counts tie exactly); OBP is
    balldontlie's (it ships no HBP or SF); OPS = OBP + SLG. A missing count
    leaves every rate built on it missing."""
    totals: dict[str, dict] = {}
    teams: dict[str, dict] = {}
    for r in rows:
        if r.get("season_type") != "regular" or r.get("postseason"):
            continue
        team = r.get("team") or {}
        abbr = team.get("abbreviation")
        if not abbr:
            continue
        ab, h, tb = r.get("batting_ab"), r.get("batting_h"), r.get("batting_tb")
        outs = outs_from_bdl_ip(r.get("pitching_ip"))
        er, ph, pbb = r.get("pitching_er"), r.get("pitching_h"), r.get("pitching_bb")
        obp = r.get("batting_obp")
        slg = _div(tb, ab) if None not in (tb, ab) else None
        totals[abbr] = {
            "bat": {"R": r.get("batting_r"), "AB": ab, "H": h, "HR": r.get("batting_hr"),
                    "SB": r.get("batting_sb"), "doubles": r.get("batting_2b"),
                    "triples": r.get("batting_3b"), "BB": r.get("batting_bb"),
                    "AVG": _div(h, ab) if None not in (h, ab) else None,
                    "OBP": obp, "SLG": slg,
                    "OPS": (obp + slg) if None not in (obp, slg) else None},
            "pit": {"outs": outs, "IP": None if outs is None else round(outs / 3.0, 3),
                    "ER": er, "H": ph, "BB": pbb, "SO": r.get("pitching_k"), "HR": r.get("pitching_hr"),
                    "HRA": r.get("pitching_hr"), "SV": r.get("pitching_sv"),
                    "W": r.get("pitching_w"), "L": r.get("pitching_l"),
                    "ERA": _div(27.0 * er, outs) if None not in (er, outs) else None,
                    "WHIP": _div(3.0 * (ph + pbb), outs) if None not in (ph, pbb, outs) else None},
        }
        teams[abbr] = team
    return totals, teams


def _div(a, b):
    return a / b if b else None


def rank(values: dict[str, Optional[float]], lower_is_better: bool) -> dict[str, tuple[int, bool]]:
    """{team: (rank, tied)}: 1 + the number of teams strictly better; a team
    without a value gets no rank. Rates compare at full precision."""
    have = {t: v for t, v in values.items() if v is not None}
    out = {}
    for t, v in have.items():
        better = sum(1 for w in have.values() if (w < v if lower_is_better else w > v))
        tied = sum(1 for w in have.values() if w == v) > 1
        out[t] = (better + 1, tied)
    return out


def payload(totals: dict[str, dict], leagues: dict[str, str]) -> dict[str, dict]:
    """Each team's stats with its MLB rank and its league rank."""
    out = {t: {"league": leagues.get(t), "stats": {}} for t in totals}
    for key, label, side, low in STATS:
        vals = {t: totals[t][side].get(key) for t in totals}
        mlb = rank(vals, low)
        by_league: dict[str, dict] = {}
        for lg in set(leagues.values()):
            by_league.update(rank({t: v for t, v in vals.items() if leagues.get(t) == lg}, low))
        for t in totals:
            m, lgr = mlb.get(t), by_league.get(t)
            out[t]["stats"][key] = {
                "label": label, "value": vals[t],
                "mlb_rank": m[0] if m else None, "mlb_tied": m[1] if m else False,
                "league_rank": lgr[0] if lgr else None, "league_tied": lgr[1] if lgr else False,
            }
    return out
