"""Team contact quality for one game: AVG, expected batting average (xBA) and
balls hit 95+ mph, per side, computed from balldontlie's `/plate_appearances`.

One function, `team_contact(pas, final=...)`, used by both the live snapshot
(`/live/games/{id}`, key `team_contact`) and the finished-game endpoint
(`/games/{bdl_id}/team-contact`), so the two can never disagree on the formula.

THE DEFINITION — Statcast's, as for a hitter's xBA:
  * xBA = (sum of xBA on balls in play) / (at-bats), a strikeout counting 0.
  * At-bats exclude walks, hit-by-pitch, sacrifice flies and bunts, catcher's
    interference, and plate appearances that ended on the bases (a runner caught
    stealing or picked off ends the PA with no at-bat).
  * balldontlie ships xBA per ball in play (`expected_batting_average` on the
    pitch put in play), so nothing is estimated here from exit velocity/angle.

⚠️ AN UNTRACKED BALL IN PLAY IS LEFT OUT OF BOTH THE SUM AND THE AT-BATS. Counting
it as 0 would call it an out, and the balls that go untracked are mostly weak
contact but not only outs (CLE on 2026-10-05: an error, .188 counted as 0, .194
left out). AVG is unaffected — it is the real H / AB. `tracked_share` says how
much of a side's contact the xBA rests on, and below `MIN_TRACKED_SHARE` the
block isn't shown at all (2015-2017 games run 83-88%).

Live, each side's xBA shows from its own first at-bat (a side whose at-bats are all
strikeouts reads .000); a side yet to bat has none, and the app draws "—". Early in
a game one ball in play moves it a lot; the app's explanation says so rather than
hiding the number.
"""
from __future__ import annotations

from typing import Optional

MIN_TRACKED_SHARE = 0.90
HARD_HIT_MPH = 95.0

HITS = {"Single", "Double", "Triple", "Home Run"}
STRIKEOUTS = {"Strikeout", "Strikeout Double Play", "Strikeout Triple Play"}
# Plate appearances that are not at-bats.
NOT_AT_BATS = {
    "Walk", "Intent Walk", "Intentional Walk", "Hit By Pitch",
    "Sac Fly", "Sac Fly Double Play", "Sac Bunt", "Sac Bunt Double Play",
    "Catcher Interference",
}
# A PA that ended on the bases ("Caught Stealing 2B", "Pickoff 1B", "Pickoff
# Caught Stealing 3B", "Runner Out") — no at-bat and no ball in play.
BASERUNNING_PREFIXES = ("Caught Stealing", "Pickoff", "Runner Out")


def _in_play_pitch(pa: dict) -> Optional[dict]:
    """The pitch put in play, if any: the one carrying contact metrics, else the
    one called in play (an untracked ball still has the call)."""
    pitches = pa.get("pitches") or []
    for q in pitches:
        if q.get("exit_velocity") is not None or q.get("expected_batting_average") is not None:
            return q
    for q in pitches:
        if (q.get("call_name") or "").lower().startswith("in play"):
            return q
    return None


def is_at_bat(result: Optional[str]) -> bool:
    if not result:
        return False
    if result in NOT_AT_BATS or result.startswith(BASERUNNING_PREFIXES):
        return False
    return True


def _side(pas: list[dict]) -> dict:
    ab = h = 0
    bip = tracked = 0
    xba_sum = 0.0
    hard = tracked_batted = 0
    for pa in pas:
        result = pa.get("result")
        q = _in_play_pitch(pa)
        # Hard-hit counts every tracked batted ball, at-bat or not (a 101-mph
        # sacrifice fly was still hit 101 mph).
        if q is not None and q.get("exit_velocity") is not None:
            tracked_batted += 1
            if q["exit_velocity"] >= HARD_HIT_MPH:
                hard += 1
        if not is_at_bat(result):
            continue
        ab += 1
        if result in HITS:
            h += 1
        if result in STRIKEOUTS:
            continue
        bip += 1
        if q is not None and q.get("expected_batting_average") is not None:
            tracked += 1
            xba_sum += float(q["expected_batting_average"])
    untracked = bip - tracked
    xba_ab = ab - untracked
    # Unrounded: the client rounds once, for display. Rounding here as well
    # would round twice — NYY on 2026-10-05 is .19548, which is .195, but .1955
    # once stored at four places and then .196.
    return {
        "ab": ab,
        "h": h,
        "avg": h / ab if ab else None,
        "xba": xba_sum / xba_ab if xba_ab > 0 else None,
        "xba_ab": xba_ab,
        "balls_in_play": bip,
        "tracked_balls_in_play": tracked,
        "tracked_share": tracked / bip if bip else None,
        # No longer a Team Stats row, but build 12 of the app decodes it as a
        # required field: dropping it would fail the block — and with it the live
        # snapshot — on that build.
        "hard_hit": hard,
        "tracked_batted_balls": tracked_batted,
    }


def team_contact(pas: list[dict], final: bool, *, box: Optional[dict] = None,
                 plays: Optional[list[dict]] = None, on_base: Optional[dict] = None,
                 game_id=None) -> dict:
    """Per-side contact block plus the display decision.

    `pas` is balldontlie's `/plate_appearances` for one game; the away side bats
    in the top half. `show` is the server's call on whether a client renders
    the block; `reason` says why not ("no_data", "untracked").

    With `box` ({"away"|"home": /stats rows}), the block also carries `stats`, the
    Team Stats rows (see team_stats.py); `plays` is the play stream RISP reads
    (None hides RISP) and `on_base` the live runners on base for the side batting.
    """
    halves: dict[str, list[dict]] = {"top": [], "bottom": []}
    for pa in pas or []:
        half = (pa.get("half_inning") or "").lower()
        if half in halves:
            halves[half].append(pa)
    away, home = _side(halves["top"]), _side(halves["bottom"])

    reason: Optional[str] = None
    if away["xba"] is None and home["xba"] is None:
        reason = "no_data"          # neither side has an at-bat (or only untracked ones)
    elif any(s["tracked_share"] is not None and s["tracked_share"] < MIN_TRACKED_SHARE
             for s in (away, home)):
        reason = "untracked"
    block = {
        "away": away,
        "home": home,
        "final": bool(final),
        "show": reason is None,
        "reason": reason,
    }
    if box is not None:
        import team_stats   # here, not at the top: team_stats imports this module
        block["stats"] = team_stats.team_stats(pas or [], box, plays, block, final,
                                               on_base=on_base, game_id=game_id)
    return block


# --- Finished games: /games/{bdl_id}/team-contact ---------------------------

# Finished games only, for the life of the process: a final game's plate
# appearances don't change. A failed fetch raises and an empty one returns
# `no_data` — neither is cached, so the next request tries again. Nor is a block
# missing a part that failed to load (the box or the play stream).
_FINAL_CACHE: dict[int, dict] = {}
MAX_PLAY_PAGES = 12


def _paged(get_json, path: str, params: dict) -> list[dict]:
    """Every page of a cursor-paginated balldontlie list."""
    rows: list[dict] = []
    cursor = None
    for _ in range(MAX_PLAY_PAGES):
        q = dict(params, per_page=100, **({"cursor": cursor} if cursor else {}))
        page = get_json(path, q) or {}
        chunk = page.get("data") or []
        rows += chunk
        cursor = (page.get("meta") or {}).get("next_cursor")
        if not cursor or len(chunk) < 100:
            return rows
    raise RuntimeError(f"{path} for {params} ran past {MAX_PLAY_PAGES} pages")


def for_game(bdl_id: int, get_json) -> dict:
    """The block for one game, fetched from balldontlie through `get_json(path,
    params)` (the backend's `data_service._bdl_get_json`). Raises on a failed
    plate-appearance fetch; the caller turns that into an HTTP error. A failed box
    or play-stream fetch leaves its rows out and the block uncached."""
    hit = _FINAL_CACHE.get(bdl_id)
    if hit is not None:
        return hit
    game = (get_json(f"games/{bdl_id}", None) or {}).get("data") or {}
    status = (game.get("status") or "").upper()
    final = "FINAL" in status
    pas = (get_json("plate_appearances", {"game_id": bdl_id, "per_page": 100}) or {}).get("data") or []
    complete = True
    try:
        rows = _paged(get_json, "stats", {"game_ids[]": bdl_id})
        ids = {"away": (game.get("away_team") or {}).get("id"), "home": (game.get("home_team") or {}).get("id")}
        box = {side: [r for r in rows if (r.get("team") or {}).get("id") == tid] for side, tid in ids.items()}
    except Exception:  # noqa: BLE001 - the contact rows still stand without the box
        box, complete = None, False
    try:
        plays = _paged(get_json, "plays", {"game_id": bdl_id})
    except Exception:  # noqa: BLE001 - RISP hides as "no_plays"
        plays, complete = None, False
    block = {"game_id": bdl_id, **team_contact(pas, final=final, box=box, plays=plays, game_id=bdl_id)}
    if final and pas and block["reason"] != "no_data" and complete:
        _FINAL_CACHE[bdl_id] = block
    return block
