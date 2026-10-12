#!/usr/bin/env python3
"""/plate_appearances is paged, live (live_service._fetch_pas) and final
(team_contact.for_game), the way /stats and /plays already are: balldontlie serves 100
rows a page, so a long extra-inning game would lose its last plate appearances to a
single-page fetch. A game under 100 rows is still exactly one call.
Run: python3 backend/tests/test_pa_paging.py
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "api"))
import live_service as ls  # noqa: E402
import team_contact as tc  # noqa: E402

results = []


def check(label, ok):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def rows(n):
    return [{"pa_number": i, "half_inning": "top" if i % 2 else "bottom", "inning": 1 + i // 8,
             "batter_id": i, "result": "Groundout", "pitches": []} for i in range(1, n + 1)]


def pages(all_rows):
    """A fake balldontlie: 100 rows a page, next_cursor while more remain."""
    calls = []

    def get(path, params):
        params = params or {}
        calls.append((path, params.get("cursor")))
        if path.startswith("games/"):
            return {"data": {"status": "STATUS_FINAL", "away_team": {"id": 1}, "home_team": {"id": 2}}}
        if path != "plate_appearances":
            return {"data": []}
        start = params.get("cursor") or 0
        chunk = all_rows[start:start + 100]
        more = start + 100 < len(all_rows)
        return {"data": chunk, "meta": {"next_cursor": start + 100} if more else {}}
    return get, calls


print("live: _fetch_pas")
ls.BDL_PACING_S = 0
for n, want in ((137, 2), (64, 1), (100, 1)):
    get, calls = pages(rows(n))

    async def fake_call(path, params, get=get):
        return get(path, params)
    ls._bdl_call = fake_call
    got = asyncio.run(ls._fetch_pas(1))
    pa_calls = [c for c in calls if c[0] == "plate_appearances"]
    check(f"{n} rows: all {n} fetched, in {len(pa_calls)} call(s) (want {want})",
          [r["pa_number"] for r in got] == list(range(1, n + 1)) and len(pa_calls) == want)

print("final: team_contact.for_game")
for n, want in ((137, 2), (64, 1)):
    tc._FINAL_CACHE.clear()
    get, calls = pages(rows(n))
    block = tc.for_game(9000 + n, get)
    pa_calls = [c for c in calls if c[0] == "plate_appearances"]
    ab = block["away"]["ab"] + block["home"]["ab"]
    check(f"{n} rows: every plate appearance counted ({ab} at-bats), {len(pa_calls)} call(s) (want {want})",
          ab == n and len(pa_calls) == want)
tc._FINAL_CACHE.clear()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
