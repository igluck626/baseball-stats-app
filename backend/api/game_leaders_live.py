"""Game Leaders, live: the game's hardest-hit balls and fastest pitches so far,
ranked here and shipped on the live snapshot (`game_leaders`).

The board a finished game shows is built by the client from balldontlie's
`/plate_appearances` (GameLeaders.swift). Live, the client has no pitch speeds —
the snapshot's contact rows carry only the ball put in play — so the server,
which polls the full feed every cycle anyway, ranks it instead. No new
balldontlie calls: this reads the `/plate_appearances` and `/stats` the live loop
already fetched.

Three rules, measured on ten finished games replayed pitch by pitch (2026-10-09):

  * TOP 3 PER CATEGORY. The final card shows ten; ten live reordered ~30 times a
    game after it filled, its tenth row turning over constantly.
  * ONLY COMPLETED AT-BATS (a plate appearance with a result). A pitch joins the
    board when its at-bat ends: ~17 changes a game after the board fills,
    against ~25 admitting each pitch as it is thrown. Every speed then matches
    the plays list's exact one for that at-bat, and a hit row always has its
    outcome.
  * NEVER WITHDRAWN. The board is the top 3 of (this cycle's events ∪ the last
    board), carried through the previous snapshot exactly as `carry_forward`
    treats a batting slot. An event leaves only when a strictly better one
    pushes it below third — never because the feed dropped its plate appearance,
    which it does (see the `missingPARow` fixture). A revised reading of the
    same event wins; memory only fills silence.

Ties go to the EARLIER event — (inning, half, pa_number, pitch index), top before
bottom — so equal speeds never swap between refreshes. GameLeaders.swift ranks
the final board by the same key, so a game's last live top 3 and its final top
3 agree when no event is missing.

The raw feed, not `team_stats.align_to_box`: that cut exists so plate-appearance
counts agree with the box score, and the board has no box-score counterpart; the
carried board already does what `hold_transient` does, permanently.
"""
from __future__ import annotations

from typing import Optional

ROWS = 3
CATEGORIES = ("hardest_hit", "fastest_pitches")


def _half(pa: dict) -> str:
    return "bottom" if "bot" in (pa.get("half_inning") or "").lower() else "top"


def _identity(e: dict) -> tuple:
    """One event: a player, a plate appearance and a pitch of it."""
    return (e["player_id"], e["inning"], e["half"], e["pa_number"], e["pitch_index"])


def rank_key(e: dict) -> tuple:
    """Fastest first; equal values, the earlier event first."""
    return (-e["value"], e["inning"], 0 if e["half"] == "top" else 1, e["pa_number"], e["pitch_index"])


def events(pas: list[dict], names: dict, *, away_id, home_id,
           play_orders: Optional[dict] = None) -> dict[str, dict[tuple, dict]]:
    """Every tracked event of the completed at-bats, by category and identity.

    `names` is player id -> display name (from `/stats`); an id without one is
    dropped rather than shown blank, as the client does. `play_orders` maps
    (inning, half, pa_number) to the at-bat's pitch rows' `order` in the play
    stream, where the two feeds agree on its pitch count — so a tapped row can
    mark its pitch; absent, the row carries no `play_order`."""
    out: dict[str, dict[tuple, dict]] = {c: {} for c in CATEGORIES}
    for pa in pas:
        if not pa.get("result"):
            continue                      # in progress: joins when it ends
        pitches = pa.get("pitches") or []
        half = _half(pa)
        orders = (play_orders or {}).get((pa.get("inning"), half, pa.get("pa_number")))
        for i, q in enumerate(pitches):
            for cat, who, value, detail, team in (
                ("hardest_hit", pa.get("batter_id"), q.get("exit_velocity"), pa.get("result"),
                 away_id if half == "top" else home_id),
                ("fastest_pitches", pa.get("pitcher_id"), q.get("release_speed"), q.get("pitch_type"),
                 home_id if half == "top" else away_id),
            ):
                if who is None or value is None or not names.get(who):
                    continue
                e = {
                    "player_id":   who,
                    "name":        names[who],
                    "team_id":     team,
                    "value":       value,
                    "detail":      detail,
                    "result":      pa.get("result"),
                    "inning":      pa.get("inning"),
                    "half":        half,
                    "pa_number":   pa.get("pa_number"),
                    "pitch_index": i,
                    "pa_pitches":  len(pitches),
                    "play_order":  orders[i] if orders and i < len(orders) else None,
                }
                out[cat][_identity(e)] = e
    return out


def build(pas: list[dict], names: dict, *, away_id, home_id,
          play_orders: Optional[dict] = None, previous: Optional[dict] = None,
          rows: int = ROWS) -> Optional[dict]:
    """The live board: per category, the top `rows` of this cycle's events and
    the last board's, a fresh reading of the same event winning. None until the
    first tracked at-bat completes, so the client shows no card."""
    fresh = events(pas, names, away_id=away_id, home_id=home_id, play_orders=play_orders)
    board: dict = {"rows": rows}
    for cat in CATEGORIES:
        pool = {_identity(e): e for e in ((previous or {}).get(cat) or [])}
        pool.update(fresh[cat])
        board[cat] = sorted(pool.values(), key=rank_key)[:rows]
    if not any(board[c] for c in CATEGORIES):
        return None
    return board
