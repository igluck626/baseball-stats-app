"""Team batting and pitching totals for a season, with MLB and league ranks — the
team page's Season Stats card.

SOURCE: balldontlie's team season totals (`/teams/season_stats`), Isaac's call
2026-10-16. They agree with balldontlie's standings on W, L and runs for all 30
clubs and with the sum of its own box lines on AB, HR and R; our per-team sums
run short wherever a game line is missing or a cell is unknown. Ours become the
nightly DRIFT CHECK instead (`drift`).

⚠️ THE ENDPOINT'S TRAPS, measured 2026-10-16:
  • `team_ids[]` is SILENTLY IGNORED — it returns all 60 rows. Never use it;
    `team_id` (singular) is the one that filters. We take every club in one
    `season=` call anyway.
  • `seasons[]` is rejected (400): it is `season=`.
  • 60 rows = each club's regular season AND postseason. Keep
    `season_type == "regular"` only.
  • Innings come in baseball notation ("1435.2" = 1435⅔).
  • No PA, HBP, SF, CS or IBB: OBP is taken as balldontlie ships it.
  • No runs allowed. Run differential comes from OUR standings instead
    (runs scored minus runs allowed, `with_run_differential`), which the
    nightly writes from balldontlie's standings.

Pure functions; main.py fetches, caches and serves.
"""
from __future__ import annotations

from typing import Optional


# (key, label, side, lower_is_better). Side "team" is Run differential, from
# our standings, not balldontlie's team totals.
STATS = [
    ("RD", "Run differential", "team", False),
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


def with_run_differential(totals: dict[str, dict], teams: dict[str, dict],
                          standings: list[dict], lahman_of: dict[int, str]) -> dict[str, dict]:
    """Each club's run differential, runs scored minus runs allowed, from our
    standings rows. A club without both numbers gets None (no rank). Returns
    `totals` with a "team" side. Pure.

    ⚠️ MATCHED ON THE LAHMAN CODE, never the standings' `bdl_id`: four 2026
    rows (KCR, TBD, SDP, SFG) carry none. `lahman_of` is balldontlie team id ->
    Lahman code (data_service._BDL_TO_LAHMAN_TEAM_MAP); the standings' team_id
    is the Lahman code ('LAA', not Retrosheet's 'ANA')."""
    by_code = {r.get("team_id"): r for r in standings if r.get("team_id")}
    for abbr, t in totals.items():
        row = by_code.get(lahman_of.get((teams.get(abbr) or {}).get("id"))) or {}
        rs, ra = row.get("runs_scored"), row.get("runs_allowed")
        t["team"] = {"RD": (rs - ra) if None not in (rs, ra) else None}
    return totals


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


# Drift thresholds: counting stats |ours - theirs| > max(2, 1%); rates by an
# absolute margin. Small enough to catch a lost game, big enough to ignore a
# float32 rounding in balldontlie's rates.
COUNT_TOL = (2, 0.01)
RATE_TOL = {"AVG": 0.003, "OBP": 0.003, "SLG": 0.003, "OPS": 0.005, "ERA": 0.03, "WHIP": 0.01}


def drift(ours: dict[str, dict], theirs: dict[str, dict]) -> dict:
    """Compare our per-team sums (keyed like `theirs`) with balldontlie's totals,
    stat by stat for the card's STATS. Returns {"drift": [(team, stat, ours,
    theirs)], "incomplete": [(team, stat)], "missing_team": [team]}. A stat we
    can't compute (an unknown cell somewhere in it) is INCOMPLETE, never drift
    and never compared as 0. Pure."""
    out = {"drift": [], "incomplete": [], "missing_team": []}
    for team in sorted(theirs):
        if team not in ours:
            out["missing_team"].append(team)
            continue
        for key, _label, side, _low in STATS:
            if side == "team":
                continue        # Run differential: balldontlie's team totals have no runs allowed
            o, t = ours[team][side].get(key), theirs[team][side].get(key)
            if t is None:
                continue
            if o is None:
                out["incomplete"].append((team, key))
                continue
            if key in RATE_TOL:
                bad = abs(o - t) > RATE_TOL[key]
            else:
                bad = abs(o - t) > max(COUNT_TOL[0], COUNT_TOL[1] * abs(t))
            if bad:
                out["drift"].append((team, key, o, t))
    return out


def _div(a, b):
    return a / b if b else None


def team_totals(bat_rows: list[dict], pit_rows: list[dict]) -> dict[str, dict]:
    """{retro team: {"bat": {...}, "pit": {...}}} summed from per-team stint rows.
    A missing value is left out of its sum (never read as 0); a total built from
    any missing value is None, so no rate is computed from a partial count."""
    out: dict[str, dict] = {}

    def add(acc, row, cols):
        for c in cols:
            v = row.get(c)
            if v is None or acc.get(c, 0) is None:
                acc[c] = None
            else:
                acc[c] = acc.get(c, 0) + v

    for r in bat_rows:
        t = out.setdefault(r["team"], {"bat": {}, "pit": {}})
        add(t["bat"], r, ("PA", "AB", "R", "H", "doubles", "triples", "HR", "RBI", "BB", "SO", "SB",
                          "HBP", "SF"))
    for r in pit_rows:
        t = out.setdefault(r["team"], {"bat": {}, "pit": {}})
        add(t["pit"], {**r, "outs": None if r.get("IP") is None else int(round(float(r["IP"]) * 3))},
            ("outs", "H", "ER", "BB", "SO", "HR", "SV", "W", "L"))
    for team, t in out.items():
        b, p = t["bat"], t["pit"]
        h, ab, bb, hbp, sf = b.get("H"), b.get("AB"), b.get("BB"), b.get("HBP"), b.get("SF")
        tb = (h + b["doubles"] + 2 * b["triples"] + 3 * b["HR"]) if None not in (
            h, b.get("doubles"), b.get("triples"), b.get("HR")) else None
        b["AVG"] = _div(h, ab) if None not in (h, ab) else None
        b["OBP"] = _div(h + bb + hbp, ab + bb + hbp + sf) if None not in (h, ab, bb, hbp, sf) else None
        b["SLG"] = _div(tb, ab) if None not in (tb, ab) else None
        b["OPS"] = (b["OBP"] + b["SLG"]) if None not in (b["OBP"], b["SLG"]) else None
        o = p.get("outs")
        p["IP"] = None if o is None else round(o / 3.0, 3)
        p["ERA"] = _div(27.0 * p["ER"], o) if None not in (p.get("ER"), o) else None
        p["WHIP"] = _div(3.0 * (p["H"] + p["BB"]), o) if None not in (p.get("H"), p.get("BB"), o) else None
        p["HRA"] = p.get("HR")
    return out


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
        vals = {t: totals[t].get(side, {}).get(key) for t in totals}
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
