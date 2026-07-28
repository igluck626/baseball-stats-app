#!/usr/bin/env python3
"""Regenerate the golden battery (ask_battery.jsonl) for the constraint-merge work.

The audit matrix that seeded this was generated ad-hoc and never persisted, so this
script IS the persisted definition. It composes:
  - COVERAGE combos (subject x stat x shape x scope x era) — the audit's spread.
  - SESSION cases — every behavior shipped this session (opponent, self-team,
    ambiguous city, season-drop, the six new count events, XBH/AB, the
    unsupported-concept guards, the pitching line, scoreless, at-bat-unit,
    rosters vs ranked).
  - KNOWN-BUG cases — Ripken consecutive games, comparisons, Ruth two-way — so the
    merge can't shift them unnoticed.

Each record: {id, q, bucket, note}. Deterministic order (no randomness) so the
file is stable across regenerations. Run: python3 gen_battery.py
"""
import json
import os

OUT = os.path.join(os.path.dirname(__file__), "ask_battery.jsonl")

rows = []
def add(bucket, q, note=""):
    rows.append({"bucket": bucket, "q": q, "note": note})

# ---- 1. BASIC COUNTS: subject x stat -----------------------------------------
# batting counts (season-stats + plays events shipped this session)
for stat in ["home runs", "strikeouts", "walks", "hits", "doubles", "triples",
             "total bases", "stolen bases", "runs", "RBIs", "at-bats",
             "extra-base hits"]:
    add("count_bat", f"How many {stat} does Aaron Judge have?", stat)
add("count_bat", "How many home runs did Barry Bonds hit?", "career HR, retired")
add("count_bat", "How many grand slams has Aaron Judge hit?", "grand slam -> HR base_state")
# pitching counts
for stat in ["strikeouts", "wins", "saves", "losses", "earned runs", "walks"]:
    add("count_pit", f"How many {stat} does Gerrit Cole have?", stat)

# ---- 2. SITUATIONAL COUNTS (the _detect_constraints injectables) -------------
add("situ", "How many home runs has Aaron Judge hit off left-handed pitchers?", "handedness->pitcher_hand")
add("situ", "How many home runs has Aaron Judge hit off right-handed pitchers?", "handedness R")
add("situ", "How many strikeouts does Gerrit Cole have against left-handed batters?", "handedness->batter_side (pit)")
add("situ", "How many home runs has Aaron Judge hit with runners in scoring position?", "base_state risp")
add("situ", "How many home runs has Aaron Judge hit with the bases loaded?", "base_state loaded")
add("situ", "How many home runs has Aaron Judge hit at home?", "home_away home = 189 baseline")
add("situ", "How many home runs has Aaron Judge hit on the road?", "home_away away")
add("situ", "How many home runs has Aaron Judge hit in a full count?", "count 3-2")
add("situ", "How many home runs has Aaron Judge hit with two strikes?", "strikes=2")
add("situ", "How many home runs has Aaron Judge hit in the 9th inning?", "inning (model-only)")
add("situ", "How many home runs has Aaron Judge hit with two outs?", "outs (model-only)")

# ---- 3. OPPONENT (this session's headline) -----------------------------------
add("opp_scoped", "How many home runs has Aaron Judge hit against the Red Sox?", "~36 scoped")
add("opp_scoped", "How many home runs has Aaron Judge hit against the Red Sox at home?", "~18 scoped+venue")
add("opp_selfteam", "How many home runs has Aaron Judge hit against the Yankees?", "self-team note, count 0")
add("opp_ambig_city", "How many home runs has Aaron Judge hit against Washington?", "ambiguous city -> decline")
add("opp_city_ok", "How many home runs has Aaron Judge hit against Detroit?", "single-franchise city -> Tigers")
add("opp_nick_city", "How many home runs has Aaron Judge hit against the Chicago Cubs?", "city+nick -> resolves")
add("opp_scoped", "How many strikeouts does Gerrit Cole have against the Astros?", "pitcher opponent inverse")

# ---- 4. SEASON SCOPE (drop-repair target for the merge) -----------------------
add("season", "How many home runs did Aaron Judge hit in 2019?", "season_single")
add("season", "How many home runs has Aaron Judge hit since 2020?", "season_start")
add("season", "How many home runs did Babe Ruth hit before 1930?", "season_end")
add("season", "How many home runs did Aaron Judge hit between 2017 and 2019?", "range")
add("season", "How many home runs did Ken Griffey Jr hit in the 1990s?", "decade range")
add("season", "How many home runs did Aaron Judge hit in 2022 off lefties?", "season+handedness")

# ---- 5. RATES ----------------------------------------------------------------
add("rates", "What is Aaron Judge's batting average?", "AVG")
add("rates", "What is Aaron Judge's OPS?", "OPS")
add("rates", "What is Aaron Judge's on-base percentage?", "OBP")
add("rates", "What is Aaron Judge's slugging percentage?", "SLG")
add("rates", "What is Aaron Judge's batting average with runners in scoring position?", "rate+risp")
add("rates", "What is Aaron Judge's OPS off left-handed pitchers?", "rate+handedness")
add("rates", "What is Aaron Judge's batting average against the Red Sox?", "rate+opponent (rates has no opp yet)")

# ---- 6. SPLITS ---------------------------------------------------------------
add("splits", "What is Aaron Judge's batting average by handedness?", "split pitcher_hand")
add("splits", "What are Aaron Judge's home and away splits?", "split home_away")
add("splits", "What is Aaron Judge's OPS by season?", "split season")

# ---- 7. LEADERBOARDS ---------------------------------------------------------
add("lb_count", "Who has hit the most home runs?", "count leaderboard")
add("lb_count", "Who hit the most home runs in 2019?", "count lb + season")
add("lb_count", "Who has hit the most home runs off left-handed pitchers?", "count lb + handedness -> plays route")
add("lb_count", "Who has the most home runs with the bases loaded?", "count lb + base_state")
add("lb_rate", "Who has the best OPS?", "rate leaderboard")
add("lb_rate", "Who has the best batting average with runners in scoring position?", "rate lb + risp")
add("lb_count", "Who has hit the most home runs against the Dodgers?", "lb + opponent (no runner filter yet)")

# ---- 8. MILESTONES -----------------------------------------------------------
add("milestone", "When did Albert Pujols hit his 600th home run?", "HR milestone")
add("milestone", "When did Derek Jeter get his 3000th hit?", "H milestone")
add("milestone", "When did Rickey Henderson steal his 1000th base?", "SB milestone")
add("milestone", "When did Rickey Henderson score his 2000th run?", "R milestone")
add("milestone", "When did Hank Aaron drive in his 2000th run?", "RBI milestone")
add("milestone", "When did Albert Pujols hit his 500th home run in 2014?", "milestone + season cap")

# ---- 9. STREAKS / SPANS ------------------------------------------------------
add("streak", "What is Joe DiMaggio's longest hitting streak?", "streak")
add("span", "What is the most home runs Aaron Judge has hit in 10 games?", "span")
add("streak", "What is the longest scoreless innings streak by Orel Hershiser?", "scoreless streak pitching line")
add("streak", "What is Cal Ripken's longest consecutive games played streak?", "KNOWN BUG: Ripken consecutive games")

# ---- 10. GAME ACHIEVEMENTS ---------------------------------------------------
add("game_ach", "How many multi-home-run games has Aaron Judge had?", "game ach")
add("game_ach", "How many multi-home-run games has Aaron Judge had against the Dodgers?", "game ach + opponent -> 2")
add("game_ach", "How many multi-home-run games has Aaron Judge had against Washington?", "game ach + ambiguous city -> decline")
add("game_ach", "How many times has Aaron Judge hit for the cycle?", "cycle")
add("game_ach", "How many times has Aaron Judge hit for the cycle against the Yankees?", "cycle + opponent")

# ---- 11. PITCHING LINE -------------------------------------------------------
add("pitch_line", "What is Gerrit Cole's ERA?", "pitching line ERA")
add("pitch_line", "What is Gerrit Cole's WHIP?", "WHIP")
add("pitch_line", "How many strikeouts per nine does Gerrit Cole average?", "SO9")
add("pitch_line", "What is Gerrit Cole's ERA in 2023?", "pitch line + season")

# ---- 12. UNSUPPORTED-CONCEPT GUARDS ------------------------------------------
add("unsupported", "How many home runs has Aaron Judge hit in day games?", "day/night -> decline")
add("unsupported", "How many home runs has Aaron Judge hit in one-run games?", "leverage -> decline")
add("unsupported", "How many home runs has Aaron Judge hit in the clutch?", "clutch -> decline")
add("unsupported", "How many walk-off home runs has Aaron Judge hit?", "walk-off -> decline")
add("unsupported", "What is Gerrit Cole's winning percentage?", "winning % -> decline")
add("unsupported", "How many plate appearances does Aaron Judge have?", "PA guard -> decline (not countable)")

# ---- 13. TWO-WAY -------------------------------------------------------------
add("two_way", "How many home runs does Shohei Ohtani have?", "two-way HR ambiguous side -> both")
add("two_way", "How many strikeouts does Shohei Ohtani have?", "two-way K -> both")
add("two_way", "How many home runs has Shohei Ohtani hit as a hitter?", "explicit side -> bat only")
add("two_way", "How many home runs did Babe Ruth hit?", "KNOWN: Ruth two-way era")
add("two_way", "How many wins did Babe Ruth have as a pitcher?", "KNOWN: Ruth pitching side")

# ---- 14. COMPARISONS (KNOWN BUG class) ---------------------------------------
add("comparison", "Who has more home runs, Aaron Judge or Mike Trout?", "KNOWN BUG: comparison")
add("comparison", "Does Aaron Judge or Giancarlo Stanton have more home runs?", "KNOWN BUG: comparison")

# ---- 15. POSTSEASON ----------------------------------------------------------
add("postseason", "How many home runs has Aaron Judge hit in the postseason?", "game_type P")
add("postseason", "How many home runs has Aaron Judge hit in the World Series?", "postseason")

# ---- 16. ROSTERS vs RANKED ---------------------------------------------------
add("roster", "Who plays for the New York Yankees?", "roster (not a leaderboard)")
add("roster", "Who is on the Dodgers roster?", "roster")

# ---- 17. ERA / DATA FLOOR ----------------------------------------------------
add("floor", "How many home runs did Babe Ruth hit off left-handed pitchers?", "pre-plays-floor situational -> decline")
add("floor", "What is Ty Cobb's batting average with runners in scoring position?", "pre-floor rate -> decline")

# ---- 18. AMBIGUOUS PLAYER NAME (the flaky one) -------------------------------
add("ambig_player", "How many strikeouts does Wagner have?", "KNOWN FLAKY: Billy Wagner vs Honus Wagner")
add("ambig_player", "How many home runs does Rodriguez have?", "ambiguous surname")

# ---- 19. AB-as-unit / display trims ------------------------------------------
add("ab_unit", "How many at-bats does Aaron Judge have?", "AB countable this session")
add("ab_unit", "How many at-bats did Aaron Judge have in 2022?", "AB + season")

# ---- 20. EXTRA subject spread (era/position variety for the matrix) ----------
for subj, st in [("Mike Trout", "home runs"), ("Mookie Betts", "stolen bases"),
                 ("Clayton Kershaw", "strikeouts"), ("Mariano Rivera", "saves"),
                 ("Ted Williams", "home runs"), ("Willie Mays", "home runs"),
                 ("Greg Maddux", "wins"), ("Tony Gwynn", "hits"),
                 ("Rickey Henderson", "stolen bases"), ("Pedro Martinez", "strikeouts")]:
    add("count_spread", f"How many {st} does {subj} have?", f"{subj} {st}")

# ---- 21. more situational + opponent cross-products for density --------------
add("situ", "How many strikeouts does Aaron Judge have with two strikes?", "batter K + strikes")
add("situ", "How many hits does Aaron Judge have with runners in scoring position?", "H + risp")
add("opp_scoped", "How many hits does Aaron Judge have against the Orioles?", "H + opponent")
add("opp_scoped", "What is Aaron Judge's OPS against the Blue Jays?", "rate + opponent")
add("season", "How many strikeouts did Gerrit Cole have in 2021?", "pit K + season")
add("rates", "What is Mike Trout's OPS in 2019?", "rate + season")
add("lb_count", "Who hit the most home runs in the 1990s?", "count lb + decade")
add("milestone", "When did Miguel Cabrera get his 3000th hit?", "H milestone 2")
add("game_ach", "How many two-home-run games did Babe Ruth have?", "game ach pre-modern")
add("pitch_line", "What is Clayton Kershaw's ERA in the postseason?", "pitch line + postseason")

# ---- 22. DENSITY: more subject x stat across eras/positions ------------------
for subj, st in [("Hank Aaron", "home runs"), ("Stan Musial", "hits"),
                 ("Rickey Henderson", "runs"), ("Cal Ripken", "home runs"),
                 ("Frank Thomas", "walks"), ("Ichiro Suzuki", "hits"),
                 ("Randy Johnson", "strikeouts"), ("Nolan Ryan", "strikeouts"),
                 ("Trevor Hoffman", "saves"), ("Roger Clemens", "wins"),
                 ("Vladimir Guerrero", "home runs"), ("David Ortiz", "home runs"),
                 ("Chipper Jones", "home runs"), ("Jeff Bagwell", "home runs"),
                 ("Craig Biggio", "hits")]:
    add("count_spread", f"How many {st} does {subj} have?", f"{subj} {st}")

# ---- 23. DENSITY: situational on varied subjects ------------------------------
add("situ", "How many home runs has Mike Trout hit off left-handed pitchers?", "Trout handedness")
add("situ", "How many home runs has Giancarlo Stanton hit with the bases loaded?", "Stanton loaded")
add("situ", "How many hits does Mookie Betts have with runners in scoring position?", "Betts risp")
add("situ", "How many home runs has Freddie Freeman hit at home?", "Freeman home")
add("situ", "How many strikeouts does Jacob deGrom have with two strikes?", "deGrom strikes")
add("situ", "How many home runs has Pete Alonso hit in the 9th inning?", "Alonso inning")

# ---- 24. DENSITY: opponent variety ------------------------------------------
add("opp_scoped", "How many home runs has Mike Trout hit against the Astros?", "Trout vs HOU")
add("opp_scoped", "How many home runs has Mookie Betts hit against the Giants?", "Betts vs SFN")
add("opp_selfteam", "How many home runs has Mike Trout hit against the Angels?", "Trout self-team (Angels)")
add("opp_ambig_city", "How many home runs has Aaron Judge hit against New York?", "NY ambiguous city")
add("opp_city_ok", "How many home runs has Aaron Judge hit against Cleveland?", "Cleveland single-franchise")
add("opp_scoped", "What is Mike Trout's batting average against the Mariners?", "rate + opponent (no runner yet)")

# ---- 25. DENSITY: season scope variety --------------------------------------
add("season", "How many home runs did Mike Trout hit in 2016?", "Trout season")
add("season", "How many hits did Ichiro Suzuki have since 2001?", "Ichiro since")
add("season", "How many strikeouts did Randy Johnson have before 2000?", "RJ before")
add("season", "How many home runs did Sammy Sosa hit between 1998 and 2001?", "Sosa range")
add("season", "How many saves did Mariano Rivera have in the 2000s?", "Rivera decade")

# ---- 26. DENSITY: rates / splits --------------------------------------------
add("rates", "What is Mookie Betts's slugging percentage?", "Betts SLG")
add("rates", "What is Freddie Freeman's OPS with runners in scoring position?", "Freeman rate+risp")
add("rates", "What is Mike Trout's on-base percentage off right-handed pitchers?", "Trout rate+hand")
add("splits", "What are Mike Trout's home and away splits?", "Trout splits home_away")
add("splits", "What is Mookie Betts's batting average by handedness?", "Betts split hand")

# ---- 27. DENSITY: leaderboards ----------------------------------------------
add("lb_count", "Who has hit the most home runs since 2015?", "count lb season_start")
add("lb_count", "Who has the most strikeouts as a pitcher?", "pit count lb")
add("lb_count", "Who has the most saves?", "saves lb")
add("lb_rate", "Who has the best batting average since 2010?", "rate lb + range")
add("lb_rate", "Who has the highest OPS with the bases loaded?", "rate lb + loaded")

# ---- 28. DENSITY: milestones / streaks / game ach ---------------------------
add("milestone", "When did Barry Bonds hit his 700th home run?", "Bonds HR milestone")
add("milestone", "When did Ichiro Suzuki get his 3000th hit?", "Ichiro H milestone")
add("streak", "What is Pete Rose's longest hitting streak?", "Rose streak")
add("game_ach", "How many multi-home-run games has Giancarlo Stanton had?", "Stanton game ach")
add("game_ach", "How many times has Mookie Betts hit for the cycle?", "Betts cycle")

# ---- 29. DENSITY: pitching line / postseason / unsupported ------------------
add("pitch_line", "What is Jacob deGrom's WHIP?", "deGrom WHIP")
add("pitch_line", "What is Justin Verlander's ERA since 2018?", "Verlander ERA + range")
add("postseason", "How many strikeouts does Clayton Kershaw have in the postseason?", "Kershaw postseason K")
add("postseason", "How many home runs has Mike Trout hit in the playoffs?", "Trout playoffs")
add("unsupported", "How many home runs has Aaron Judge hit in extra innings?", "extra innings -> decline")
add("unsupported", "How many home runs has Aaron Judge hit at night?", "night -> decline")
add("unsupported", "What is Aaron Judge's WAR?", "WAR unsupported -> decline")

# ---- 30. DENSITY: two-way / comparison / roster -----------------------------
add("two_way", "How many strikeouts does Shohei Ohtani have as a pitcher?", "Ohtani explicit pit side")
add("comparison", "Who has more strikeouts, Randy Johnson or Nolan Ryan?", "comparison pitchers")
add("roster", "Who plays for the Boston Red Sox?", "Red Sox roster")

if __name__ == "__main__":
    # stable ids by position; dedupe on normalized q
    seen = set()
    out = []
    for i, r in enumerate(rows):
        key = r["q"].lower().strip()
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": f"q{len(out):03d}", **r})
    with open(OUT, "w") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(out)} questions to {OUT}")
    from collections import Counter
    c = Counter(r["bucket"] for r in out)
    for b, n in sorted(c.items()):
        print(f"  {b:16s} {n}")
