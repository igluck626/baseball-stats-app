#!/usr/bin/env python3
"""The nightly re-reads the last 30 days of finals (scoring changes arrive days
later), each date through the same save that keeps known values.

Standalone, no pytest. Run: <backend python> backend/tests/test_reread_window.py
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "scripts")]
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # unreachable on purpose
import nightly_update as nu                                         # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


seen = []
orig, orig_ids = nu.data_service.save_bdl_gamelogs_for_date, nu._ids_with_current_season
nu.data_service.save_bdl_gamelogs_for_date = lambda d, **kw: (seen.append(d), {"status": "ok", "games": 0})[1]
nu._ids_with_current_season = lambda model, year: set()          # the informational count only
try:
    nu._update_gamelogs(2026)
finally:
    nu.data_service.save_bdl_gamelogs_for_date, nu._ids_with_current_season = orig, orig_ids
today = datetime.date.today()
check("31 dates: the last 30 days plus today", len(seen) == 31, len(seen))
check("oldest first, ending today", seen[0] == (today - datetime.timedelta(days=30)).isoformat() and seen[-1] == today.isoformat(),
      (seen[0], seen[-1]))
check("every date once", len(set(seen)) == 31)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
