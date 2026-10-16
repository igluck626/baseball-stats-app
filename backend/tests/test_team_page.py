#!/usr/bin/env python3
"""The team page's reads — `/teams/{bdl_id}/schedule` and `/teams/{bdl_id}/roster`
— and the rules in team_page.py behind them.

⚠️ THE CHECKS THAT MATTER:
  • A season is over when no regular-season game is left to play; a postponed
    game that was never made up doesn't hold it open (the Dodgers have one).
  • How it ended comes from the bracket: missed the postseason, out in a round
    (with the series score and the opponent), champion, or still alive.
  • A roster line is the player's line FOR THIS TEAM only when his stints add up
    to his season row; a traded player's line is computed from his stint for this
    team, without WAR (stored per season only); anything that doesn't add up is
    his whole season, labelled "all_teams".
  • Innings are compared as outs from decimal thirds (6.333), the stint tables'
    convention.

Inputs: real /games rows for LAD and COL (fixtures/bdl_games_2026_lad_col.json)
and our own /postseason/bracket output for 2026 (fetched mid-NLCS) and 2025
(complete). Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_team_page.py
"""
import copy
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import team_page as tp                                            # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def load(name):
    with open(os.path.join(HERE, "fixtures", name)) as f:
        return json.load(f)


GAMES = load("bdl_games_2026_lad_col.json")["data"]
B2026, B2025 = load("bracket_2026.json"), load("bracket_2025.json")
LAD = [g for g in GAMES if 14 in (g["home_team"]["id"], g["away_team"]["id"])]
COL = [g for g in GAMES if 9 in (g["home_team"]["id"], g["away_team"]["id"])]

print("the schedule")
unk = copy.deepcopy(LAD[-1])
unk["id"] = 99999999
unk["away_team"]["id"], unk["away_team"]["abbreviation"] = -1, "UNK"
games = tp.schedule_games(LAD + [unk, LAD[0]])
check("a placeholder game (team -1 / UNK) is dropped and a repeat counted once",
      len(games) == len(LAD) and all(g["id"] != 99999999 for g in games), len(games))
check("games are in start order", [g["date"] for g in games] == sorted(g["date"] for g in games))
check("spring training stays (the hero's strip shows it in March)",
      any(g["season_type"] == "spring_training" for g in games))
check("the Dodgers' regular season is over, their one postponed game notwithstanding",
      tp.regular_season_over(LAD) and any(g["status"] == "STATUS_POSTPONED" for g in LAD))
open_season = copy.deepcopy(LAD)
last_regular = max((g for g in open_season if g["season_type"] == "regular"), key=lambda g: g["date"])
last_regular["status"] = "STATUS_SCHEDULED"
check("a regular-season game still to play keeps the season open", not tp.regular_season_over(open_season))
day = tp.eastern_date(last_regular["date"])
check("a game today that isn't over is a game today", tp.has_game_today(open_season, day))
check("a finished one is not", not tp.has_game_today(LAD, day))
check("a 7:10pm PT start is that Eastern date, not the next UTC day",
      tp.eastern_date("2026-09-28T02:10:00.000Z") == datetime.date(2026, 9, 27))

print("how the season ended")
check("Dodgers, mid-NLCS: alive", tp.season_result(B2026, "LAD", True) == {"state": "alive"})
check("Rockies: missed the postseason", tp.season_result(B2026, "COL", True) == {"state": "missed_postseason"})
check("Astros: out in the AL Wild Card, 0-2 to the White Sox",
      tp.season_result(B2026, "HOU", True) == {"state": "eliminated", "round_name": "AL Wild Card",
                                                "wins": 0, "losses": 2, "opponent": "CHW"},
      tp.season_result(B2026, "HOU", True))
check("Padres: out in the NLDS, 1-3 to the Brewers (a series they won comes first)",
      tp.season_result(B2026, "SD", True)["state"] == "eliminated"
      and tp.season_result(B2026, "SD", True)["opponent"] == "MIL"
      and (tp.season_result(B2026, "SD", True)["wins"], tp.season_result(B2026, "SD", True)["losses"]) == (1, 3),
      tp.season_result(B2026, "SD", True))
check("2025 Dodgers: champions, 4-3 over Toronto",
      tp.season_result(B2025, "LAD", True) == {"state": "champion", "round_name": "World Series",
                                                "wins": 4, "losses": 3, "opponent": "TOR"},
      tp.season_result(B2025, "LAD", True))
check("2025 Blue Jays: out in the World Series, 3-4",
      tp.season_result(B2025, "TOR", True)["state"] == "eliminated"
      and tp.season_result(B2025, "TOR", True)["round_name"] == "World Series")
check("regular season still going: in progress, whatever the bracket says",
      tp.season_result(B2026, "COL", False) == {"state": "in_progress"})
check("no bracket: the season is over, and nothing more is claimed",
      tp.season_result(None, "COL", True) == {"state": "season_over"})
untrusted = copy.deepcopy(B2026)
untrusted["leagues"]["NL"]["trusted"] = False
check("seeds that can't be trusted can't say a team missed out",
      tp.season_result(untrusted, "COL", True) == {"state": "season_over"})
check("next season's opener is its first regular-season game (spring training skipped)",
      tp.first_regular_game(LAD)["season_type"] == "regular"
      and tp.first_regular_game(LAD)["date"] == min(g["date"] for g in LAD if g["season_type"] == "regular"))
check("no games listed yet: no opener", tp.first_regular_game([]) is None)

print("team codes")
for abbr, code in (("LAD", "LAN"), ("LAA", "ANA"), ("OAK", "ATH"), ("KC", "KCA"),
                   ("CHW", "CHA"), ("WSH", "WAS"), ("SD", "SDN"), ("ARI", "ARI")):
    check(f"balldontlie {abbr} is the stint tables' {code}", tp.retro_code(abbr) == code, tp.retro_code(abbr))
check("pitchers are recognised however balldontlie spells them",
      tp.is_pitcher("RP") and tp.is_pitcher("Relief Pitcher") and not tp.is_pitcher("DH"))

print("roster lines")
SEASON_BAT = {"AB": 500, "H": 150, "doubles": 30, "triples": 2, "HR": 20, "RBI": 80, "BB": 50,
              "HBP": 5, "SF": 4, "BA": 0.300, "OPS": 0.880, "WAR": 4.1, "G": 140}
one_team = [{"team": "LAN", **{k: SEASON_BAT[k] for k in tp._BAT_CHECK}, "G": 140}]
scope, line = tp.roster_stats("LAN", one_team, {"standard": SEASON_BAT, "advanced": SEASON_BAT}, False)
check("one team, adds up: the season row is this team's line, WAR included",
      scope == "team" and line["avg"] == 0.300 and line["war"] == 4.1 and line["hr"] == 20, (scope, line))
traded = [{"team": "NYA", "AB": 300, "H": 90, "doubles": 18, "triples": 1, "HR": 12, "RBI": 50,
           "BB": 30, "HBP": 3, "SF": 2, "G": 80},
          {"team": "LAN", "AB": 200, "H": 60, "doubles": 12, "triples": 1, "HR": 8, "RBI": 30,
           "BB": 20, "HBP": 2, "SF": 2, "G": 60}]
scope, line = tp.roster_stats("LAN", traded, {"standard": SEASON_BAT, "advanced": SEASON_BAT}, False)
tb = 60 + 12 + 2 + 24
check("traded, adds up: this team's stint (.300 over 200 AB), not the season",
      scope == "team" and line["avg"] == 0.3 and line["hr"] == 8 and line["g"] == 60, (scope, line))
check("its OPS is built from the stint: (H+BB+HBP)/(AB+BB+HBP+SF) + TB/AB",
      abs(line["ops"] - ((60 + 20 + 2) / (200 + 20 + 2 + 2) + tb / 200)) < 1e-9, line["ops"])
check("and WAR is null: it is stored per season, not per team", line["war"] is None)
short = copy.deepcopy(traded)
short[0]["H"] -= 1
scope, line = tp.roster_stats("LAN", short, {"standard": SEASON_BAT, "advanced": SEASON_BAT}, False)
check("stints one hit short of the season: his whole season, labelled all teams",
      scope == "all_teams" and line["avg"] == 0.300 and line["war"] == 4.1, (scope, line))
scope, _ = tp.roster_stats("LAN", [dict(traded[0], AB=500, H=150, doubles=30, triples=2, HR=20,
                                         RBI=80, BB=50, HBP=5, SF=4)],
                           {"standard": SEASON_BAT, "advanced": SEASON_BAT}, False)
check("no stint for this team (all his games elsewhere): all teams", scope == "all_teams")
check("no stints at all: all teams",
      tp.roster_stats("LAN", [], {"standard": SEASON_BAT, "advanced": SEASON_BAT}, False)[0] == "all_teams")
check("no season row: no line", tp.roster_stats("LAN", one_team, None, False) == (None, None))

SEASON_PIT = {"IP": 58.0, "H": 40, "ER": 17, "BB": 20, "SO": 74, "W": 5, "ERA": 2.64, "WHIP": 1.03,
              "WAR": 1.2, "G": 72}
p_one = [{"team": "LAN", "IP": 58.0, "H": 40, "ER": 17, "BB": 20, "SO": 74, "W": 5, "G": 72}]
scope, line = tp.roster_stats("LAN", p_one, {"standard": SEASON_PIT, "advanced": SEASON_PIT}, True)
check("a pitcher whose stint adds up: the season line, WAR included",
      scope == "team" and line["era"] == 2.64 and line["war"] == 1.2 and line["w"] == 5, (scope, line))
p_traded = [{"team": "SEA", "IP": 20.333, "H": 15, "ER": 7, "BB": 8, "SO": 25, "W": 2, "G": 25},
            {"team": "LAN", "IP": 37.667, "H": 25, "ER": 10, "BB": 12, "SO": 49, "W": 3, "G": 47}]
scope, line = tp.roster_stats("LAN", p_traded, {"standard": SEASON_PIT, "advanced": SEASON_PIT}, True)
check("innings add up as outs from thirds: 20.333 + 37.667 = 58.0", scope == "team", (scope, line))
check("this team's ERA from its stint: 27 x 10 ER / 113 outs",
      abs(line["era"] - 27 * 10 / 113) < 1e-9 and line["war"] is None, line)
notation = copy.deepcopy(p_traded)
notation[0]["IP"], notation[1]["IP"] = 20.1, 37.2    # baseball notation, the nightly's old output
check("innings in baseball notation don't add up, so the line falls back to all teams",
      tp.roster_stats("LAN", notation, {"standard": SEASON_PIT, "advanced": SEASON_PIT}, True)[0] == "all_teams")

print("the endpoints")
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
import main                                                       # noqa: E402

fetches = []


def fake_pages(params, cap=60):
    fetches.append(dict(params))
    if params["seasons[]"] == 2027:
        return []
    other = copy.deepcopy(COL[0])
    other["home_team"]["id"], other["away_team"]["id"] = 2, 3      # a row for neither team
    return {14: LAD, 9: COL}[params["team_ids[]"]] + [other]


main.data_service._bdl_games_page_all = fake_pages
main._postseason_inputs = lambda season: ([], [])
main.postseason_series.build_bracket = lambda games, standings, season: B2026
main._cache.clear()
s = main.team_schedule(9, season=2026, fresh=False)
check("schedule: only the team's games (a row for two other teams is dropped)",
      len(s["games"]) == len(COL) and s["abbreviation"] == "COL", len(s["games"]))
check("schedule: the Rockies' season result is 'missed the postseason'",
      s["season_result"] == {"state": "missed_postseason"}, s["season_result"])
check("schedule: next season looked up, none listed yet",
      s["next_season_first_game"] is None and any(f["seasons[]"] == 2027 for f in fetches))
n = len(fetches)
main.team_schedule(9, season=2026, fresh=False)
check("schedule: a second request is served from cache", len(fetches) == n)
s = main.team_schedule(14, season=2026, fresh=False)
check("schedule: the Dodgers are alive, so no next-season lookup",
      s["season_result"] == {"state": "alive"} and s["next_season_first_game"] is None)
try:
    main.team_schedule(31, season=2026, fresh=False); code = 200
except main.HTTPException as e:
    code = e.status_code
check("schedule: an unknown team id is a 404", code == 404, code)

ACTIVE = [{"id": 1, "full_name": "A Hitter", "position": "CF", "jersey": "7", "team": {"id": 14, "abbreviation": "LAD"}},
          {"id": 2, "full_name": "A Reliever", "position": "Relief Pitcher", "jersey": "51", "team": {"id": 14, "abbreviation": "LAD"}},
          {"id": 3, "full_name": "Unmapped", "position": "SS", "jersey": None, "team": {"id": 14, "abbreviation": "LAD"}},
          {"id": 4, "full_name": "Wrong Team", "position": "C", "jersey": None, "team": {"id": 9, "abbreviation": "COL"}}]
INJ = [{"player": {"id": 5, "full_name": "Hurt Guy", "position": "SP", "team": {"id": 14}},
        "status": "60-Day-IL", "type": "Elbow", "return_date": "2027-02-01T00:00:00.000Z"}]


def fake_bdl(path, params):
    return {"data": ACTIVE if path == "players/active" else INJ, "meta": {}}


main.data_service._bdl_get_json = fake_bdl
main.connection.db_available = lambda: True
main.data_service.get_player_by_bdl_id = lambda b: {1: {"player_id": 101, "name": "A Hitter"},
                                                    2: {"player_id": 102, "name": "A Reliever"},
                                                    5: {"player_id": 105, "name": "Hurt Guy"}}.get(b)
main.data_service._current_year = lambda: 2026
main._roster_rows = lambda players, year: (
    {(101, False): SEASON_BAT, (102, True): SEASON_PIT},
    {(101, False): traded, (102, True): p_one})
main._cache.clear()
r = main.team_roster(14)
by = {p["bdl_id"]: p for p in r["players"]}
check("roster: a row for another team is dropped", set(by) == {1, 2, 3}, sorted(by))
check("roster: the team code is the stint tables' (LAD -> LAN)", r["team_code"] == "LAN")
check("roster: a traded hitter's line is his Dodgers stint", by[1]["scope"] == "team" and by[1]["stats"]["hr"] == 8)
check("roster: 'Relief Pitcher' is a pitcher, with his pitching line",
      by[2]["is_pitcher"] and by[2]["stats"]["era"] == 2.64)
check("roster: a player with no id of ours is listed, without a line",
      by[3]["player"] is None and by[3]["stats"] is None and by[3]["scope"] is None)
check("roster: the injury list carries status, return date and our id",
      r["injuries"][0]["status"] == "60-Day-IL" and r["injuries"][0]["player"]["player_id"] == 105)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
