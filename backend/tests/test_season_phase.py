#!/usr/bin/env python3
"""`season_phase` and `GET /season/phase` — a season's Opening Day and last
regular-season day, from balldontlie's schedule.

⚠️ THE CHECKS THAT MATTER:
  • Opening Day is the first date by which at least 15 teams have played:
    an overseas opener (2025 Tokyo, 03-18) or a lone opening-night game
    (2026, 03-25) is not Opening Day.
  • The last regular day is the SCHEDULE's last regular-season game, not the
    latest final so far (mid-season that is always yesterday).
  • Spring training and the postseason are not the regular season; a postponed
    or canceled slot is not a game on that day. Dates are Eastern.
  • A REQUEST NEVER WALKS BALLDONTLIE: the endpoint reads the stored value,
    stale if a refresh failed, 503 only if none was ever derived; the refresh
    runs in the background and retries.

Standalone, no pytest. Needs the backend's Python (3.10+).
Run: <backend python> backend/tests/test_season_phase.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import season_phase as sp                                         # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


def g(iso, home, away, kind="regular", status="STATUS_FINAL"):
    return {"date": iso, "season_type": kind, "status": status,
            "home_team": {"id": home}, "away_team": {"id": away}}


def slate(day_utc, first_team=1, games=15):
    """A full day: `games` games, two new teams each."""
    return [g(day_utc, first_team + 2 * i, first_team + 2 * i + 1) for i in range(games)]


# 2025-shaped: a Tokyo opener (2 teams, 03-18/19), then the domestic slate 03-27.
TOKYO = ([g("2025-03-18T10:10:00.000Z", 1, 2), g("2025-03-19T10:10:00.000Z", 2, 1)]
         + slate("2025-03-27T17:05:00.000Z") + [g("2025-09-28T19:10:00.000Z", 3, 4)])
# 2026-shaped: one opening-night game (8:05pm ET 03-25 = 00:05 UTC 03-26), then the full slate on 03-26.
OPENING_NIGHT = [g("2026-03-26T00:05:00.000Z", 1, 2)] + slate("2026-03-26T17:05:00.000Z", first_team=3, games=14)
SEASON_2026 = (
    [g("2026-02-20T18:05:00.000Z", 1, 2, "spring_training")] + OPENING_NIGHT
    + [g("2026-09-27T19:10:00.000Z", 5, 6),
       g("2026-09-28T23:05:00.000Z", 7, 8, status="STATUS_POSTPONED"),   # a rained-out slot, never played
       g("2026-09-29T00:05:00.000Z", 9, 10, status="STATUS_CANCELED"),
       g("2026-09-29T18:00:00.000Z", 1, 2, "postseason")])

print("Opening Day: 15 teams")
d = sp.derive(TOKYO)
check("2025: the Tokyo Series (2 teams) is not Opening Day; 03-27 is", d["opening_day"] == "2025-03-27", d)
d = sp.derive(SEASON_2026)
check("2026: the lone opening-night game (2 teams) is not; the full slate on 03-26 is",
      d["opening_day"] == "2026-03-26", d)
check("the count is cumulative: 14 teams, then 1 more the next day, reaches 15 on that day",
      sp.derive(slate("2026-04-01T17:05:00.000Z", games=7) + [g("2026-04-02T17:05:00.000Z", 15, 16)])["opening_day"]
      == "2026-04-02")
spring = [dict(x, season_type="spring_training") for x in slate("2026-03-01T17:05:00.000Z")]
check("a spring-training slate never makes Opening Day", sp.derive(spring)["opening_day"] is None)

print("last regular day")
check("09-27: postponed / canceled slots and the postseason don't count",
      sp.derive(SEASON_2026)["last_regular_day"] == "2026-09-27")
mid = sp.derive(OPENING_NIGHT + [g("2026-09-27T19:10:00.000Z", 5, 6, status="STATUS_SCHEDULED")])
check("mid-season the end is the last SCHEDULED game, not the latest final", mid["last_regular_day"] == "2026-09-27", mid)
check("a late West Coast game counts on its Eastern date", sp.eastern_date("2026-09-28T02:10:00.000Z").isoformat() == "2026-09-27")
none = sp.derive([g("2027-02-20T18:05:00.000Z", 1, 2, "spring_training")])
check("no regular-season schedule yet: both null", none == {"opening_day": None, "last_regular_day": None}, none)
check("an unparseable date is skipped, not a crash", sp.derive([g("garbage", 1, 2)] + TOKYO)["opening_day"] == "2025-03-27")

print("GET /season/phase never walks balldontlie")
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
import main                                                       # noqa: E402

calls = []
PAGES = [{"data": SEASON_2026[:8], "meta": {"next_cursor": 7}}, {"data": SEASON_2026[8:], "meta": {}}]


def fake_bdl(path, params):
    calls.append((path, dict(params)))
    if params["seasons[]"] != [2026]:
        return {"data": [], "meta": {}}
    return PAGES[1] if params.get("cursor") == 7 else PAGES[0]


main.data_service._bdl_get_json = fake_bdl
sp._store.clear()
try:
    main.season_phase_span(season=2026)
    check("before any derivation: 503", False, "no exception")
except main.HTTPException as e:
    check("before any derivation: 503", e.status_code == 503, e.status_code)
check("  ...and the request made NO balldontlie call", calls == [], calls)

check("a background refresh pages the season through its cursor", sp.refresh(fake_bdl, [2026]) and len(calls) == 2, calls)
check("  ...asking for the season, without the ignored postseason / start_date filters",
      all(c[1].get("seasons[]") == [2026] and "postseason" not in c[1] and "start_date" not in c[1] for c in calls), calls)
n = len(calls)
body = main.season_phase_span(season=2026)
check("a request answers from the stored value",
      body == {"season": 2026, "opening_day": "2026-03-26", "last_regular_day": "2026-09-27"}, body)
for _ in range(5):
    main.season_phase_span(season=2026)
check("  ...and six requests made NO balldontlie call", len(calls) == n, len(calls) - n)


def failing(path, params):
    raise RuntimeError("balldontlie down")


check("a failed refresh reports failure", sp.refresh(failing, [2026]) is False)
check("  ...and requests keep the stale value", main.season_phase_span(season=2026)["last_regular_day"] == "2026-09-27")

attempts = []


def flaky(path, params):
    attempts.append(1)
    if len(attempts) == 1:
        raise RuntimeError("first try fails")
    return fake_bdl(path, params)


sp._store.clear()
sp.start_refresh("test", flaky, retries=2, retry_seconds=0).join(timeout=10)
check("start_refresh retries after a failure and then stores the value",
      sp.cached(2026) is not None and len(attempts) > 1, (sp.cached(2026), len(attempts)))
check("current_seasons: this Eastern year and last", sp.current_seasons(
    __import__("datetime").datetime(2027, 1, 1, 3, 0, tzinfo=__import__("datetime").timezone.utc)) == [2026, 2025])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
