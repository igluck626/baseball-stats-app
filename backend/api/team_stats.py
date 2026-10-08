"""Team Stats for one game: per side, from balldontlie's box (/stats rows), plate
appearances (/plate_appearances) and play stream (/plays). Shipped inside the
team-contact block as `stats` (see team_contact.py), on the live snapshot and from
/games/{bdl_id}/team-contact.

Rows, in display order:
  avg       H / AB, from the box.
  xba       the team-contact xBA (PA feed); shown under team_contact's own rule (a side
            has an at-bat, and 90% of each side's balls in play are tracked).
  hard_hit  balls hit 95+ mph (PA feed).
  hr, bb, so, sb, pitches   summed from the box.
  risp      hits and at-bats with runners in scoring position (PA feed + plays).
  lob       team left on base, by the box-score proof.
  dp        double plays turned by this side's defense (PA feed).

⚠️ RISP COUNTS AN AT-BAT WHERE A RUNNER REACHES SCORING POSITION DURING IT, not only
one that starts that way — Baseball-Reference's count, measured on three 2026
postseason games: a steal, a wild pitch, a balk or a pickoff error that puts a man on
second during an at-bat makes it an at-bat with RISP (LAD 0 for 4, TB 1 for 13, CHW
3 for 8, each one or two short when counted from the starting runners alone). The PA
feed records only the runners at the START, and the play stream carries no base
state at all — no field on any row — so the in-at-bat moves are read from the play
stream's event TEXT. That reading FAILS CLOSED: an event the classifier doesn't
recognise hides the RISP row for the game, with the reason in the block and a
warning in the log, so a change of wording shows up instead of quietly undercounting.

⚠️ TEAM LOB IS NOT THE SUM OF THE PLAYERS' LOB (LAD on 2026-10-06: 9 against 20 — a
player's LOB counts runners he stranded, the team's counts runners stranded). It is
the box-score proof: plate appearances = runs + left on base + outs made, so
LOB = PA - R - outs, where outs are the opposing pitchers' recorded outs (24 when the
home side didn't bat in the 9th). Live, runners still on base in the half being
played are not "left" yet and are subtracted too. Matches Baseball-Reference for all
six sides of the three games.

PA-FEED ROWS (xba, hard_hit, risp, dp) hide when that side's completed plate
appearances in the feed don't number the box's plate appearances: the feed drops a
row now and then, and a count read from an incomplete feed would be quietly wrong.
Only TRUE plate appearances count — not the row the feed writes for an inning that
ended on the bases (MIL @ SD 2026-10-06: Taylor's "Caught Stealing 2B", which hid
these rows for the whole game). LOB's plate appearances come from the box, which
never counted that row.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import team_contact as tc

log = logging.getLogger(__name__)

ROW_ORDER = ["avg", "xba", "hard_hit", "hr", "risp", "lob", "bb", "so", "sb", "dp", "pitches"]
PA_FEED_ROWS = {"xba", "hard_hit", "risp", "dp"}
DP_WORDS = ("Double Play", "GIDP", "Triple Play")

# --- the in-at-bat event classifier ------------------------------------------------
# Rows that are not a pitch, not the batter's own result and not an inning marker.
MARKERS = {"Start Batter/Pitcher", "End Batter/Pitcher"}   # plus any type naming an inning
# Event types that move runners. Their text must be recognised, or RISP hides.
RUNNER_TYPES = {"Stolen Base", "Caught Stealing", "Wild Pitch", "Passed Ball", "Balk", "Pick Off",
                "Pickoff", "Fielders Indifference", "Defensive Indifference", "Error",
                "Other Advance", "Runner Out", "Pickoff Caught Stealing"}
# Text that says a runner moved, for a row of a type not listed above.
_RUNNER_HINT = re.compile(r"\b(stole|scored|to (second|third|home)|advanced|picked off|caught stealing|out at)\b", re.I)
# Notes that move no runner (substitutions, positions).
_SUBSTITUTION = re.compile(
    r"\b(relieved|hit for|ran for)\b"
    r"|\bin (left|center|right) field\b"
    r"|\bat (first base|second base|third base|shortstop|catcher|pitcher)\b"
    r"|\bas designated hitter\b|\bcatching\b|\bpitching\b", re.I)
_FIELDERS = (r"(pitcher|catcher|first baseman|second baseman|third baseman|shortstop|"
             r"left fielder|center fielder|right fielder|first|second|third)")
# One clause of a runner-moving event; each maps to whether it puts a runner in
# scoring position (True) or not (False). Anything else is unrecognised.
_CLAUSES: list[tuple[re.Pattern, bool]] = [
    # a fielding credit after a caught stealing: "pitcher to third", "catcher to shortstop"
    (re.compile(rf"^{_FIELDERS}( to {_FIELDERS})+\.?$", re.I), False),
    (re.compile(r"^.+? stole (second|third)\.?$", re.I), True),
    (re.compile(r"^.+? stole home\.?$", re.I), False),
    (re.compile(r"^.+? to (second|third)( on .+)?\.?$", re.I), True),
    (re.compile(r"^.+? scored( on .+)?\.?$", re.I), False),
    (re.compile(r"^.+? caught stealing (second|third|home)\.?$", re.I), False),
    (re.compile(r"^.+? picked off (first|second|third)\.?$", re.I), False),
    # a dropped third strike: "Edman struck out, Edman to first on wild pitch by Miller."
    (re.compile(r"^.+? struck out( swinging| looking)?\.?$", re.I), False),
    (re.compile(r"^.+? to first( on .+)?\.?$", re.I), False),
]


# A replay review appended to an event: "New York Yankees challenged: call on the
# field was upheld." An upheld or confirmed call leaves the play as described; an
# overturned one may not, so only these are recognised (anything else fails closed).
_REVIEW_KEPT = re.compile(r"\s+[^.]+ challenged( \([^)]+\))?: call on the field was (upheld|confirmed)\.?$", re.I)


def classify(text: str) -> Optional[bool]:
    """True if the event puts a runner in scoring position, False if it moves runners
    without doing so, None if any part of it isn't recognised."""
    # Drop a trailing upheld/confirmed review note, then read clause by clause. (Not
    # sentence by sentence: "Mesa Jr. stole second." has a period mid-sentence.)
    body = _REVIEW_KEPT.sub("", (text or "").strip())
    clauses = [c.strip() for c in body.split(",") if c.strip()]
    if not clauses:
        return None
    hit = False
    for c in clauses:
        for pattern, puts_in_sp in _CLAUSES:
            if pattern.match(c):
                hit = hit or puts_in_sp
                break
        else:
            return None
    return hit


def _in_at_bat_moves(plays: list[dict], half: str) -> tuple[dict, list[str]]:
    """{(inning, batter_id, n): True} for each at-bat (the n-th by that batter in the
    inning) during which a non-batting event put a runner in scoring position, and
    the texts of events the classifier didn't recognise."""
    moved: dict = {}
    unrecognised: list[str] = []
    cur = None            # (inning, batter, n) of the at-bat in progress
    seen: dict = {}       # (inning, batter) -> at-bats started so far
    last_event_text = None
    for p in sorted(plays, key=lambda r: r.get("order") or 0):
        if (p.get("inning_type") or "").lower() != half:
            continue
        typ, text, batter = p.get("type"), p.get("text") or "", p.get("batter_id")
        if typ == "Start Batter/Pitcher":
            key = (p.get("inning"), batter)
            # a pitching change mid-at-bat repeats the marker for the same batter
            if cur is None or cur[:2] != key:
                seen[key] = seen.get(key, 0) + 1
                cur = (*key, seen[key])
            continue
        if typ in MARKERS or "Inning" in (typ or ""):
            if typ == "End Inning":
                cur = None
            continue
        if batter is not None:
            if typ == "Play Result":
                cur = None   # the at-bat is over
            continue
        # a non-batting row: an event, its duplicate "Play Result", a substitution, or
        # a note that moves nobody
        if typ == "Play Result" and (text == last_event_text or _SUBSTITUTION.search(text)):
            continue
        if typ not in RUNNER_TYPES and typ != "Play Result" and not _RUNNER_HINT.search(text):
            continue
        last_event_text = text
        verdict = classify(text)
        if verdict is None:
            unrecognised.append(text)
        elif verdict and cur is not None:
            moved[cur] = True
    return moved, unrecognised


def _risp(pas_side: list[dict], plays: list[dict], half: str) -> tuple[Optional[tuple[int, int]], list[str]]:
    moved, unrecognised = _in_at_bat_moves(plays, half)
    if unrecognised:
        return None, unrecognised
    h = ab = 0
    seen: dict = {}
    for pa in sorted(pas_side, key=lambda r: (r.get("inning") or 0, r.get("pa_number") or 0)):
        key = (pa.get("inning"), pa.get("batter_id"))
        seen[key] = seen.get(key, 0) + 1
        if not tc.is_at_bat(pa.get("result")):
            continue
        if pa.get("runner_on_second") or pa.get("runner_on_third") or moved.get((*key, seen[key])):
            ab += 1
            h += pa.get("result") in tc.HITS
    return (h, ab), []


def _is_plate_appearance(result) -> bool:
    """A completed plate appearance: a result that isn't a base-running out (the same
    rule `team_contact.is_at_bat` uses to keep those out of the at-bats)."""
    return bool(result) and not result.startswith(tc.BASERUNNING_PREFIXES)


def _sum(rows: list[dict], key: str) -> int:
    return sum((r.get(key) or 0) for r in rows)


def team_stats(pas: list[dict], box: dict, plays: Optional[list[dict]], contact: dict,
               final: bool, on_base: Optional[dict] = None, game_id=None) -> dict:
    """The `stats` object. `box` is {"away": [stat rows], "home": [stat rows]} from
    /stats; `plays` the play stream (None when it couldn't be fetched); `contact` the
    team_contact block; `on_base` live runners on base for the side batting now,
    {"away"|"home": n}."""
    halves = {"away": "top", "home": "bottom"}
    hidden: dict = {}
    unrecognised: list[str] = []
    out = {}
    feed_ok = True
    for side, half in halves.items():
        other = "home" if side == "away" else "away"
        mine = [r for r in box.get(side, []) if r.get("at_bats") is not None or r.get("plate_appearances") is not None]
        pitchers_mine = [r for r in box.get(side, []) if r.get("pitch_count") is not None or r.get("pitching_outs")]
        pitchers_theirs = box.get(other, [])
        side_pas = [p for p in pas if (p.get("half_inning") or "").lower() == half]
        # True plate appearances only: the feed also writes a row for an inning that
        # ended on the bases ("Caught Stealing 2B" with a batter at the plate), which
        # isn't one — the batter leads off the next inning, and the box agrees.
        completed = [p for p in side_pas if _is_plate_appearance(p.get("result"))]
        opp_side_pas = [p for p in pas if (p.get("half_inning") or "").lower() != half and p.get("result")]
        pa_box = _sum(mine, "plate_appearances")
        if len(completed) != pa_box:
            feed_ok = False
        ab, h, r = _sum(mine, "at_bats"), _sum(mine, "hits"), _sum(mine, "runs")
        outs = _sum(pitchers_theirs, "pitching_outs")
        lob = pa_box - r - outs - ((on_base or {}).get(side) or 0)
        out[side] = {
            "avg": h / ab if ab else None,
            "xba": contact[side]["xba"],
            "hard_hit": contact[side]["hard_hit"],
            "hr": _sum(mine, "hr"),
            "risp_h": None, "risp_ab": None,
            "lob": max(lob, 0),
            "bb": _sum(mine, "bb"),
            "so": _sum(mine, "k"),
            "sb": _sum(mine, "stolen_bases"),
            "dp": sum(1 for p in opp_side_pas if any(w in (p.get("result") or "") for w in DP_WORDS)),
            "pitches": sum((r.get("pitch_count") or 0) for r in pitchers_mine),
            "pa": pa_box, "feed_pa": len(completed),
        }
    if not feed_ok:
        for k in PA_FEED_ROWS:
            hidden[k] = "pa_feed_mismatch"
    if "risp" not in hidden:
        if plays is None:
            hidden["risp"] = "no_plays"
        else:
            for side, half in halves.items():
                res, bad = _risp([p for p in pas if (p.get("half_inning") or "").lower() == half and p.get("result")],
                                 plays, half)
                unrecognised += bad
                if res is not None:
                    out[side]["risp_h"], out[side]["risp_ab"] = res
            if unrecognised:
                hidden["risp"] = "unrecognised_event"
                for side in halves:
                    out[side]["risp_h"] = out[side]["risp_ab"] = None
                log.warning("team stats: RISP hidden for game %s — unrecognised in-at-bat event(s): %s",
                            game_id, unrecognised[:5])
    if "xba" not in hidden and not contact.get("show"):
        hidden["xba"] = contact.get("reason") or "not_shown"
    any_pa = out["away"]["pa"] or out["home"]["pa"]
    rows = [k for k in ROW_ORDER if k not in hidden] if any_pa else []
    return {"rows": rows, "hidden": hidden, "away": out["away"], "home": out["home"],
            "unrecognised_events": unrecognised[:5]}
