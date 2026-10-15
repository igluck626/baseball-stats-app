#!/usr/bin/env python3
"""`recent_team_form` — L10 is a team's last ten regular-season games, however
long ago they were, not the last ten inside the recent window.

⚠️ THE CHECKS THAT MATTER:
  • After the regular season ends the 20-day window holds only a few games, and
    on 2026-10-15 L10 read 3-0 for the Dodgers and 1-1 for the Rockies; the
    truth (balldontlie's own standings) was 8-2 and 2-8. The games before the
    window supply the rest.
  • The postseason does not count: the Dodgers lost three NLCS games in the
    window, and L10 is still the regular season's 8-2.
  • Mid-season the answer is unchanged; in the first days of a season the
    partial record stays partial (never padded to ten).

Input is real `/games` rows for LAD (14) and COL (9), fetched 2026-10-15 and
trimmed by dropping fields only (fixtures/bdl_games_2026_lad_col.json). The
fetch is stubbed: `seasons[]` returns them all, `dates[]` the ones on those
dates, which is what balldontlie honours.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_recent_form_l10.py
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import data_service as ds                                         # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


with open(os.path.join(HERE, "fixtures", "bdl_games_2026_lad_col.json")) as f:
    GAMES = json.load(f)["data"]


def fake_pages(params, cap=60):
    if "seasons[]" in params:
        return list(GAMES)
    dates = set(params["dates[]"])
    return [g for g in GAMES if g["date"][:10] in dates]


ds._bdl_games_page_all = fake_pages
LAD, COL = "14", "9"


def form(day):
    ds._store.clear()
    return ds.recent_team_form(2026, today=datetime.date.fromisoformat(day))["teams"]


def l10(teams, team):
    t = teams[team]
    return f'{t["last_ten_w"]}-{t["last_ten_l"]}', t["last_ten_games"], t["games_played"]


def expected(team_id, day):
    """Independently: the last ten decided regular-season finals on or before `day`."""
    rows = sorted((g for g in GAMES
                   if g["season_type"] == "regular" and g["status"] == "STATUS_FINAL"
                   and g["date"][:10] <= day
                   and team_id in (g["home_team"]["id"], g["away_team"]["id"])),
                  key=lambda g: g["date"])
    last = rows[-10:]
    wins = sum(1 for g in last
               if (g["home_team"]["id"] == team_id)
               == (g["home_team_data"]["runs"] > g["away_team_data"]["runs"]))
    return f"{wins}-{len(last) - wins}", len(last), len(rows)


print("After the regular season (2026-10-15, NLCS under way)")
t = form("2026-10-15")
check("Dodgers L10 is 8-2 over ten games, 162 played", l10(t, LAD) == ("8-2", 10, 162), l10(t, LAD))
check("Rockies L10 is 2-8 over ten games, 162 played", l10(t, COL) == ("2-8", 10, 162), l10(t, COL))

print("Two months after the season, the window empty")
t = form("2026-12-01")
check("Dodgers still 8-2", l10(t, LAD)[:2] == ("8-2", 10), l10(t, LAD))
check("Rockies still 2-8", l10(t, COL)[:2] == ("2-8", 10), l10(t, COL))

print("Mid-season, and the season's first days")
for day in ("2026-07-01", "2026-07-16", "2026-09-27", "2026-03-27", "2026-04-02"):
    t = form(day)
    for team_id, key, name in ((14, LAD, "Dodgers"), (9, COL, "Rockies")):
        exp = expected(team_id, day)
        check(f"{day} {name} {exp[0]} over {exp[1]}, {exp[2]} played", l10(t, key) == exp,
              f"got {l10(t, key)}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
