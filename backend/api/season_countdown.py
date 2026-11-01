"""The offseason countdown on Home and Scores (Isaac, 2026-10-31): from the end
of the World Series to Opening Day, the next of three milestones and the days
until it.

  1. Pitchers & catchers report — typed in by hand once a year
     (data/season_milestones.json); no source we use publishes it.
  2. Spring training games begin — the first spring training game on
     balldontlie's schedule (`season_phase`).
  3. Opening Day — `season_phase`'s Opening Day.

A date in the file overrides the schedule for that milestone, so a lockout that
moves things is one edit. A milestone whose date isn't known yet still shows,
named, with no date — until a LATER milestone has one, which then shows instead.
On a milestone's own day `days` is 0.

⚠️ NO COUNTDOWN WHILE A SEASON IS ON. From July to December it starts only once
the World Series is over (balldontlie's bracket, via `postseason_series`); from
January to June it runs until Opening Day. "Don't know if it's over" (a
balldontlie failure) is no countdown, never a wrong one.
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Callable, Optional

MILESTONES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "season_milestones.json")

STAGES = (
    ("pitchers_catchers_report", "Pitchers & catchers report"),
    ("spring_training_start", "Spring training games begin"),
    ("opening_day", "Opening Day"),
)


def load_milestones(path: str = MILESTONES_FILE) -> dict:
    """{season: {stage: "YYYY-MM-DD"}} from the hand-typed file; {} if unreadable."""
    try:
        raw = json.load(open(path))
    except (OSError, ValueError):
        return {}
    out: dict[int, dict] = {}
    for season, stages in raw.items():
        if not season.isdigit() or not isinstance(stages, dict):
            continue
        out[int(season)] = {k: v["date"] for k, v in stages.items()
                            if isinstance(v, dict) and isinstance(v.get("date"), str)}
    return out


def countdown(today: datetime.date, milestones: dict, phase: Callable[[int], Optional[dict]],
              season_over: Callable[[int], Optional[bool]]) -> dict:
    """The countdown for `today` (Eastern): {"season", "stage", "label", "date",
    "days", "source"}, every field None when there is none.

    `phase(season)` is `season_phase.cached`; `season_over(year)` says whether
    that year's World Series is over (None when unknown)."""
    none = {"season": None, "stage": None, "label": None, "date": None, "days": None, "source": None}
    if today.month >= 7:
        if season_over(today.year) is not True:
            return none
        season = today.year + 1
    else:
        season = today.year
    typed = milestones.get(season, {})
    derived = phase(season) or {}
    stages = []
    for key, label in STAGES:
        iso, source = typed.get(key), "manual"
        if iso is None:
            iso, source = derived.get(key), "schedule"
        stages.append((key, label, datetime.date.fromisoformat(iso) if iso else None, source if iso else None))
    for i, (key, label, date, source) in enumerate(stages):
        if date is not None and date < today:
            continue                                          # already passed
        if date is None and any(d is not None and d >= today for _, _, d, _ in stages[i + 1:]):
            continue                                          # unknown, and a later one is known
        return {"season": season, "stage": key, "label": label,
                "date": date.isoformat() if date else None,
                "days": (date - today).days if date else None, "source": source}
    return none                                               # Opening Day has passed
