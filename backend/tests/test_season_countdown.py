#!/usr/bin/env python3
"""`season_countdown` and `GET /season/countdown` — the offseason countdown on
Home and Scores (Isaac, 2026-10-31).

⚠️ THE CHECKS THAT MATTER:
  • It starts only once the World Series is over (July-December), and runs
    January-June until Opening Day; "can't tell" is no countdown.
  • Milestones in order: pitchers & catchers report (the hand-typed file),
    first spring training game, Opening Day (the schedule). The file
    overrides the schedule; a milestone on its own day reads 0 days.
  • An unknown date still shows, named and dateless — unless a later
    milestone has a date.
  • The real file: 2027 pitchers & catchers report 2027-02-09.

Standalone, no pytest. Run: <backend python> backend/tests/test_season_countdown.py
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
import season_countdown as sc                                     # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


D = datetime.date
TYPED = {2027: {"pitchers_catchers_report": "2027-02-09"}}
NO_SCHEDULE = lambda s: {"opening_day": None, "last_regular_day": None, "spring_training_start": None}
SCHEDULE = lambda s: {"opening_day": "2027-03-25", "last_regular_day": "2027-09-26",
                      "spring_training_start": "2027-02-19"} if s == 2027 else None
OVER, ON, UNKNOWN = (lambda y: True), (lambda y: False), (lambda y: None)


def cd(today, typed=TYPED, phase=NO_SCHEDULE, over=OVER):
    return sc.countdown(today, typed, phase, over)


print("when it runs")
r = cd(D(2026, 11, 1))
check("the day after the World Series: pitchers & catchers report in 100 days, Tue Feb 9",
      (r["season"], r["stage"], r["date"], r["days"], r["source"]) ==
      (2027, "pitchers_catchers_report", "2027-02-09", 100, "manual"), r)
check("World Series not over: no countdown", cd(D(2026, 10, 28), over=ON)["stage"] is None)
check("can't tell (balldontlie down): no countdown, never a wrong one", cd(D(2026, 11, 1), over=UNKNOWN)["stage"] is None)
check("January needs no World Series check", cd(D(2027, 1, 5), over=UNKNOWN)["stage"] == "pitchers_catchers_report")

print("the three milestones in order")
check("report day itself: 0 days", cd(D(2027, 2, 9))["days"] == 0)
r = cd(D(2027, 2, 10), phase=SCHEDULE)
check("after reporting: spring training games begin, from the schedule",
      (r["stage"], r["date"], r["days"], r["source"]) == ("spring_training_start", "2027-02-19", 9, "schedule"), r)
r = cd(D(2027, 2, 20), phase=SCHEDULE)
check("during spring training: Opening Day", (r["stage"], r["date"], r["days"]) == ("opening_day", "2027-03-25", 33), r)
check("after Opening Day: no countdown", cd(D(2027, 3, 26), phase=SCHEDULE)["stage"] is None)
check("  ...nor in June", cd(D(2027, 6, 30), phase=SCHEDULE)["stage"] is None)

print("unknown dates")
r = cd(D(2027, 2, 10))
check("after reporting, no schedule yet: spring training, named, no date",
      (r["stage"], r["label"], r["date"], r["days"]) == ("spring_training_start", "Spring training games begin", None, None), r)
r = cd(D(2026, 11, 1), typed={}, phase=SCHEDULE)
check("no typed report date but a schedule: the first known milestone (spring training)",
      r["stage"] == "spring_training_start" and r["days"] == 110, r)
r = cd(D(2026, 11, 1), typed={})
check("nothing known at all: pitchers & catchers, named, no date",
      (r["stage"], r["date"]) == ("pitchers_catchers_report", None), r)
check("the schedule not derived yet (None): same as unknown",
      cd(D(2027, 2, 10), phase=lambda s: None)["stage"] == "spring_training_start")

print("the file overrides the schedule (a lockout moving dates)")
moved = {2027: {"pitchers_catchers_report": "2027-03-02", "opening_day": "2027-04-15"}}
r = cd(D(2027, 3, 3), typed=moved, phase=SCHEDULE)
check("a typed Opening Day replaces the schedule's", (r["stage"], r["date"], r["source"]) == ("opening_day", "2027-04-15", "manual"), r)

print("the real file")
real = sc.load_milestones()
check("2027 pitchers & catchers report 2027-02-09", real.get(2027, {}).get("pitchers_catchers_report") == "2027-02-09", real)
check("an unreadable file is no dates, not a crash", sc.load_milestones("/nonexistent.json") == {})

print("GET /season/countdown")
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
import main                                                       # noqa: E402
main._postseason_inputs = lambda year: ([], [])
main.postseason_series.build_series = lambda g, s, y: [{"round": "WS", "is_over": True}]
body = main.season_countdown_now()
check("answers with the six fields", set(body) == {"season", "stage", "label", "date", "days", "source"}, body)


def down(year):
    raise RuntimeError("balldontlie down")


main._postseason_inputs = down
real_now = main.datetime.datetime


class Oct(real_now):
    @classmethod
    def now(cls, tz=None):
        return real_now(2026, 11, 1, 12, tzinfo=tz)


main.datetime.datetime = Oct
try:
    check("balldontlie down in November: every field null",
          all(v is None for v in main.season_countdown_now().values()))
finally:
    main.datetime.datetime = real_now

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if results and all(results) else 1)
