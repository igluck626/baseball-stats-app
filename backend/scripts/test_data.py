#!/Users/igluck/baseball-stats-app/backend/venv/bin/python
"""Fetch current season batting stats and print top 10 hitters by WAR."""

import datetime
import pybaseball

pybaseball.cache.enable()

current_year = datetime.date.today().year

# Season opener is typically late March; 3.1 PA/team game is the standard qualifier.
season_start = datetime.date(current_year, 3, 20)
days_played = max((datetime.date.today() - season_start).days, 1)
estimated_team_games = days_played * (162 / 183)  # 183-day regular season
min_pa = max(50, int(3.1 * estimated_team_games))

print(f"Fetching {current_year} batting stats (min {min_pa} PA)...")

batting = pybaseball.batting_stats_bref()  # no arg = current season
war_df = pybaseball.bwar_bat(return_all=True)

print(f"\nbatting_stats_bref columns ({len(batting.columns)}):")
print("  " + ", ".join(batting.columns.tolist()))

print(f"\nbwar_bat columns ({len(war_df.columns)}):")
print("  " + ", ".join(war_df.columns.tolist()))

war_current = war_df[war_df["year_ID"] == current_year][["mlb_ID", "WAR", "WAR_off", "WAR_def"]]

merged = batting.merge(war_current, left_on="mlbID", right_on="mlb_ID", how="inner")
qualified = merged[merged["PA"] >= min_pa].copy()

top10 = (
    qualified[["Name", "Tm", "G", "PA", "BA", "OBP", "SLG", "OPS", "HR", "RBI", "WAR"]]
    .sort_values("WAR", ascending=False)
    .head(10)
    .reset_index(drop=True)
)
top10.index += 1

print(f"\nTop 10 qualified hitters by WAR — {current_year} season\n")
print(top10.to_string())
