#!/Users/igluck/baseball-stats-app/backend/venv/bin/python
"""Debug career stats merge for 2025 and 2026 — Mike Trout player_id 545361."""

import pandas as pd
import pybaseball

pybaseball.cache.enable()

PLAYER_ID = 545361

print("=" * 60)
print("batting_stats_bref 2025 — rows containing 'Trout'")
print("=" * 60)
try:
    bref_2025 = pybaseball.batting_stats_bref(2025)
    trout_2025 = bref_2025[bref_2025["Name"].str.contains("Trout", case=False, na=False)]
    print(f"Total rows in 2025 bref: {len(bref_2025)}")
    print(f"Trout rows found: {len(trout_2025)}")
    if not trout_2025.empty:
        pd.set_option("display.max_columns", None)
        pd.set_option("display.width", 200)
        print(trout_2025.to_string())
        print(f"\nmlbID column type: {bref_2025['mlbID'].dtype}")
        print(f"Trout mlbID value: {trout_2025['mlbID'].values}")
    else:
        print("No Trout rows found in 2025 bref.")
        print("Sample columns:", list(bref_2025.columns))
        print("Sample mlbID values:", bref_2025["mlbID"].head(5).values if "mlbID" in bref_2025.columns else "NO mlbID COLUMN")
except Exception as e:
    print(f"ERROR fetching 2025 bref: {e}")

print()
print("=" * 60)
print("batting_stats_bref 2026 — rows containing 'Trout'")
print("=" * 60)
try:
    bref_2026 = pybaseball.batting_stats_bref(2026)
    trout_2026 = bref_2026[bref_2026["Name"].str.contains("Trout", case=False, na=False)]
    print(f"Total rows in 2026 bref: {len(bref_2026)}")
    print(f"Trout rows found: {len(trout_2026)}")
    if not trout_2026.empty:
        pd.set_option("display.max_columns", None)
        pd.set_option("display.width", 200)
        print(trout_2026.to_string())
        print(f"\nmlbID column type: {bref_2026['mlbID'].dtype}")
        print(f"Trout mlbID value: {trout_2026['mlbID'].values}")
    else:
        print("No Trout rows found in 2026 bref.")
        print("Sample columns:", list(bref_2026.columns))
        print("Sample mlbID values:", bref_2026["mlbID"].head(5).values if "mlbID" in bref_2026.columns else "NO mlbID COLUMN")
except Exception as e:
    print(f"ERROR fetching 2026 bref: {e}")

print()
print("=" * 60)
print("bwar_bat — Trout rows for 2025 and 2026")
print("=" * 60)
try:
    bwar = pybaseball.bwar_bat(return_all=True)
    trout_war = bwar[bwar["name_common"].str.contains("Trout", case=False, na=False)]
    recent = trout_war[trout_war["year_ID"].isin([2024, 2025, 2026])]
    print(f"Trout rows in bwar_bat (2024-2026):")
    print(recent[["year_ID", "team_ID", "lg_ID", "mlb_ID", "name_common", "WAR", "pitcher"]].to_string())
    print(f"\nmlb_ID column type: {bwar['mlb_ID'].dtype}")
    print(f"Trout mlb_ID values: {recent['mlb_ID'].values}")
except Exception as e:
    print(f"ERROR fetching bwar_bat: {e}")

print()
print("=" * 60)
print("Merge key comparison")
print("=" * 60)
print(f"player_id parameter (int): {PLAYER_ID}  type={type(PLAYER_ID)}")

try:
    # Simulate what data_service does
    bwar = pybaseball.bwar_bat(return_all=True)
    trout_war = bwar[bwar["mlb_ID"] == PLAYER_ID]
    print(f"\nbwar_bat match using == {PLAYER_ID}: {len(trout_war)} rows")
    if not trout_war.empty:
        print(f"mlb_ID values matched: {trout_war['mlb_ID'].values}  dtype={trout_war['mlb_ID'].dtype}")

    trout_war_str = bwar[bwar["mlb_ID"] == float(PLAYER_ID)]
    print(f"bwar_bat match using == float({PLAYER_ID}): {len(trout_war_str)} rows")
except Exception as e:
    print(f"ERROR in merge key check: {e}")

try:
    bref_2025 = pybaseball.batting_stats_bref(2025)
    if "mlbID" in bref_2025.columns:
        trout_bref = bref_2025[bref_2025["mlbID"] == PLAYER_ID]
        print(f"\nbref_2025 match using mlbID == {PLAYER_ID}: {len(trout_bref)} rows")
        trout_bref_str = bref_2025[bref_2025["mlbID"] == str(PLAYER_ID)]
        print(f"bref_2025 match using mlbID == str({PLAYER_ID}): {len(trout_bref_str)} rows")
        if not bref_2025.empty:
            print(f"bref_2025 mlbID dtype: {bref_2025['mlbID'].dtype}")
            print(f"Sample bref_2025 mlbID values: {bref_2025['mlbID'].head(3).values}")
except Exception as e:
    print(f"ERROR in bref match check: {e}")
