"""Live-game proxy service (Phase 1).

Polls balldontlie for in-progress MLB games on a fixed schedule, assembles ONE
unified per-game snapshot (score + linescore + inning/count/base state +
batter/pitcher + recent/scoring plays + per-player lines), and caches it. The
read endpoints (`/live/games`, `/live/games/{id}`) serve straight from this
cache and NEVER call balldontlie — so upstream load is O(games), not O(users),
and every UI element a client renders comes from one consistent snapshot
(this is what fixes the score-vs-plays skew).

SINGLE-WORKER ASSUMPTION
------------------------
This uses an in-process asyncio background loop + the in-process TTL cache
(`cache.py`). That is correct ONLY while the backend runs a single uvicorn
worker (confirmed: `Procfile`/`Dockerfile` invoke `uvicorn main:app` with no
`--workers`). If the app is ever scaled to multiple workers/instances, EACH
worker would start its own loop (N× the balldontlie calls) and keep its own
cache (inconsistent reads). At that point: move the loop to a single owner
(a dedicated worker / Railway service) and the cache to Redis (the public
getters here stay the same). `start_live_loop()` also guards against starting
the loop more than once within a process.

All balldontlie calls use the server-side BDL_KEY (via data_service's client);
no client key is involved.
"""

import asyncio
import datetime
import logging
import urllib.error
from typing import Any, Optional

import data_service
from cache import cache as _cache

log = logging.getLogger("live_service")

# --- Tunables -------------------------------------------------------------

REFRESH_INTERVAL_S = 10.0      # poll live games every 10s
SCHEDULE_INTERVAL_S = 60.0     # when nothing is live, only re-check the slate this often
LIVE_CACHE_TTL_S = 30          # > refresh interval, so a snapshot survives between
                               # cycles but self-evicts ~30s after a game stops refreshing
LINEUP_CACHE_TTL_S = 6 * 3600  # lineups are STATIC for a game (set pre-first-pitch,
                               # never change), so fetch once per game and reuse the
                               # cached copy every cycle — one /lineups call per game
                               # total, not per cycle. TTL comfortably outlasts a game.
MAX_PLAY_PAGES = 16           # /plays is cursor-paginated (100/page, earliest-
                              # first). Per-pitch rows make a 9-inning game
                              # ~300-700 plays, so follow next_cursor up to this
                              # many pages (~1600 plays) to assemble the FULL
                              # game. Set high enough to cover even an 18-inning
                              # marathon so the cap is never the limiter in
                              # practice — critical because pagination is
                              # oldest-first, so hitting the cap would drop the
                              # MOST RECENT plays (and any late scoring plays).
                              # If it's ever hit we log a warning (see below).
BDL_PACING_S = data_service._BDL_RATE_LIMIT_SLEEP   # 0.22s between BDL calls
BDL_429_BACKOFF_S = 2.0
BDL_429_RETRIES = 2

_SUMMARY_KEY = "live:summary"


def _game_key(game_id: int) -> str:
    return f"live:game:{game_id}"


def _lineup_key(game_id: int) -> str:
    return f"live:lineup:{game_id}"


# --- BDL fetch helpers (run the sync urllib client off the event loop) ----

async def _bdl_call(path: str, params: dict) -> dict:
    """Await a balldontlie GET via the shared sync client, off the event loop.
    Retries briefly on HTTP 429 so a transient rate-limit doesn't drop a cycle."""
    for attempt in range(BDL_429_RETRIES + 1):
        try:
            return await asyncio.to_thread(data_service._bdl_get_json, path, params)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < BDL_429_RETRIES:
                log.warning("BDL 429 on %s (attempt %d) — backing off %.1fs",
                            path, attempt + 1, BDL_429_BACKOFF_S)
                await asyncio.sleep(BDL_429_BACKOFF_S)
                continue
            raise


def _et_dates_window() -> list[str]:
    """Yesterday / today / tomorrow in MLB-local (ET) dates — covers games that
    cross midnight ET so a late West-coast game still resolves as 'today'."""
    today = datetime.datetime.now(data_service._MLB_LOCAL_TZ).date()
    return [(today + datetime.timedelta(days=d)).isoformat() for d in (-1, 0, 1)]


async def _fetch_slate() -> list[dict]:
    """One `/games` call for the ET ±1 day window — carries each game's status,
    teams, live score (team_data.runs), and inning-by-inning grid."""
    data = await _bdl_call("games", {"dates[]": _et_dates_window(), "per_page": 100})
    return data.get("data") or []


async def _fetch_plays(game_id: int) -> list[dict]:
    """Full-game play feed. balldontlie /plays is cursor-paginated (100/page,
    earliest-first), so a single page only covers the opening innings. We follow
    `meta.next_cursor` (capped at MAX_PLAY_PAGES) to assemble the WHOLE game —
    otherwise the client's 'All plays' view is missing most of the game."""
    rows: list[dict] = []
    cursor: Optional[int] = None
    for _page in range(MAX_PLAY_PAGES):
        params: dict[str, Any] = {"game_id": game_id, "per_page": 100}
        if cursor is not None:
            params["cursor"] = cursor
        data = await _bdl_call("plays", params)
        chunk = data.get("data") or []
        rows.extend(chunk)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor or len(chunk) < 100:
            break                            # cursor exhausted → we have the full game
        await asyncio.sleep(BDL_PACING_S)    # pace between pages, same as between calls
    else:
        # Ran the full page budget without exhausting the cursor — i.e. a game
        # longer than MAX_PLAY_PAGES*100 plays. Pagination is oldest-first, so the
        # rows we kept are the EARLIEST and we've dropped the most-recent action +
        # any late scoring plays. The cap is sized so this shouldn't happen for any
        # real game, so make it loud rather than silently serving stale plays.
        if cursor is not None:
            log.warning("game %s exceeded %d play pages (>%d plays) — most recent "
                        "plays truncated; raise MAX_PLAY_PAGES",
                        game_id, MAX_PLAY_PAGES, MAX_PLAY_PAGES * 100)
    return rows


async def _fetch_pas(game_id: int) -> list[dict]:
    data = await _bdl_call("plate_appearances", {"game_id": game_id, "per_page": 100})
    return data.get("data") or []


async def _fetch_stats(game_id: int) -> list[dict]:
    # /stats uses the plural game_ids[] (unlike /plays + /plate_appearances).
    data = await _bdl_call("stats", {"game_ids[]": game_id, "per_page": 100})
    return data.get("data") or []


async def _fetch_lineup(game_id: int) -> list[dict]:
    # /lineups also filters on the PLURAL game_ids[] (singular game_id is silently
    # ignored, same trap as /stats). Each row: batting_order (1-9), position,
    # is_probable_pitcher, player{id,...}, team{...}.
    data = await _bdl_call("lineups", {"game_ids[]": game_id, "per_page": 100})
    return data.get("data") or []


async def _get_lineup_cached(game_id: int) -> tuple[list[dict], bool]:
    """The starting lineup drives batting order — but /stats itself carries no
    order and ships teams interleaved, so we join against /lineups. Lineups are
    set pre-first-pitch and never change, so fetch ONCE per game and reuse the
    cached copy every subsequent cycle (this is what keeps the cost to ~1 call
    per game, not per cycle).

    Returns (lineup, made_bdl_call). On a fetch error returns ([], True) so the
    caller falls back to the current unsorted order for this cycle and retries
    next cycle — a missing lineup must never crash the assembly."""
    key = _lineup_key(game_id)
    cached = _cache.get(key)
    if cached is not None:
        return cached, False
    await asyncio.sleep(BDL_PACING_S)   # pace the one-time fetch like any other call
    try:
        lineup = await _fetch_lineup(game_id)
    except Exception:
        log.warning("lineup fetch failed for game %s — unsorted batting this cycle", game_id)
        return [], True
    # Only cache a non-empty lineup; an empty result (e.g. not posted pre-game
    # yet) should be retried next cycle rather than pinned for LINEUP_CACHE_TTL_S.
    if lineup:
        _cache.set(key, lineup, LINEUP_CACHE_TTL_S)
    return lineup, True


# --- Assembly -------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _norm_status(raw: Optional[str]) -> str:
    s = (raw or "").upper()
    if "IN_PROGRESS" in s:
        return "in_progress"
    if "FINAL" in s:
        return "final"
    return "scheduled"


def _norm_half(inning_type: Optional[str]) -> Optional[str]:
    """Normalize a play's `inning_type` to "top"/"bottom", or None for the
    inning-transition markers ("Mid"/"End") and blanks — so the situation's
    half can only ever land on a real side."""
    t = (inning_type or "").lower()
    if "top" in t:
        return "top"
    if "bot" in t:
        return "bottom"
    return None


def _first(d: dict, *keys: str) -> Any:
    """First present (non-None) value among `keys` — used for balldontlie
    fields whose exact key spelling we can't pin from the client alone
    (e.g. pitching hits ships as `p_hits` or `pitching_hits`)."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _team_block(team: dict, team_data: dict) -> dict:
    bdl_id = team.get("id")
    td = team_data or {}
    return {
        "bdl_id":       bdl_id,
        "team_code":    data_service._BDL_TO_LAHMAN_TEAM_MAP.get(bdl_id),
        "abbreviation": team.get("abbreviation"),
        "name":         team.get("name"),
        "location":     team.get("location"),
        "league":       team.get("league"),
        "division":     team.get("division"),
        "runs":         td.get("runs"),
        "hits":         td.get("hits"),
        "errors":       td.get("errors"),
    }


def _play_block(p: dict) -> dict:
    return {
        "order":       p.get("order"),
        "inning":      p.get("inning"),
        "half":        (p.get("inning_type") or "").lower() or None,
        "outs":        p.get("outs"),
        "away_score":  p.get("away_score"),
        "home_score":  p.get("home_score"),
        "scoring":     bool(p.get("scoring_play")),
        "score_value": p.get("score_value"),
        "batter_id":   p.get("batter_id"),
        "pitcher_id":  p.get("pitcher_id"),
        "text":        p.get("text"),
        "type":        p.get("type"),
    }


def _team_name_candidates(team: dict) -> set[str]:
    """Every spelling balldontlie might use for a team's name, so a /stats row's
    `team_name` can be matched whichever form it ships. The live /stats feed sets
    `team_name` to the FULL name (e.g. 'Chicago White Sox') — which is the team's
    `display_name`, NOT its `name` ('White Sox') — so matching `name` alone drops
    every row."""
    cands: set[str] = set()
    for k in ("display_name", "name", "full_name", "short_display_name"):
        v = team.get(k)
        if v:
            cands.add(v)
    loc, nm = team.get("location"), team.get("name")
    if loc and nm:
        cands.add(f"{loc} {nm}")
    return cands


def _side_for_stat(stat: dict, home_team: dict, away_team: dict) -> Optional[str]:
    player = stat.get("player") or {}
    team = player.get("team") or {}
    tid = team.get("id")
    if tid is not None and tid == home_team.get("id"):
        return "home"
    if tid is not None and tid == away_team.get("id"):
        return "away"
    # Live /stats rows carry no team id (player.team is null), only `team_name`
    # as the full display name — match that against either side's name forms.
    name = stat.get("team_name")
    if name:
        if name in _team_name_candidates(home_team):
            return "home"
        if name in _team_name_candidates(away_team):
            return "away"
    return None


def _bat_row(stat: dict) -> Optional[dict]:
    # Only players who came to the plate.
    if stat.get("at_bats") is None and stat.get("plate_appearances") is None:
        return None
    p = stat.get("player") or {}
    return {
        "id":       p.get("id"),
        "name":     p.get("full_name"),
        "position": p.get("position"),
        # Filled from /lineups during assembly (null for substitutes not in the
        # starting nine). The client sorts server-side already, but exposing it
        # keeps the ordering self-describing.
        "batting_order": None,
        "ab":  stat.get("at_bats"),
        "r":   stat.get("runs"),
        "h":   stat.get("hits"),
        "rbi": stat.get("rbi"),
        "hr":  stat.get("hr"),
        "bb":  stat.get("bb"),
        "k":   stat.get("k"),
        "avg": stat.get("avg"),
        "obp": stat.get("obp"),
        "slg": stat.get("slg"),
    }


def _pit_row(stat: dict) -> Optional[dict]:
    # Only players who pitched (ip present and non-zero).
    ip = stat.get("ip")
    if ip in (None, 0, "0", "0.0", ""):
        return None
    p = stat.get("player") or {}
    return {
        "id":   p.get("id"),
        "name": p.get("full_name"),
        "ip":   ip,
        "h":    _first(stat, "p_hits", "pitching_hits"),
        "r":    _first(stat, "p_runs", "pitching_runs"),
        "er":   stat.get("er"),
        "bb":   _first(stat, "p_bb", "pitching_bb"),
        "k":    _first(stat, "p_k", "pitching_k"),
        "hr":   _first(stat, "p_hr", "pitching_hr"),
        "era":  stat.get("era"),
        "w":    stat.get("wins"),
        "l":    stat.get("losses"),
        "sv":   stat.get("saves"),
    }


# ── Pure derivations (§2 of docs/live_redesign_phase1.md) ──────────────────
# Each is a pure function of the feed rows it receives, so it can be unit-tested
# against a saved fixture (tests/test_live_derivations.py). Redesign principle:
# PLAYS are the single source of truth for live STATE — inning, half, outs,
# count, score, per-inning grid, hits, scoring plays, batter/pitcher id. Names,
# base runners, errors, and per-player stat lines stay on their own feeds.

# Hit outcomes are matched by SUFFIX (not an exact set) because balldontlie
# types hits with descriptive prefixes: "Bunt Single", "Infield Single", "Ground
# Rule Double", "Inside The Park Home Run", … all of which are still hits. Suffix
# matching is a strict superset of {Single, Double, Triple, Home Run} and catches
# every variant. Safe against the obvious false positives — "Double Play" /
# "Triple Play" end in "play", not the hit word (see `_is_hit_type`).
_HIT_SUFFIXES = ("single", "double", "triple", "home run")
# Words that appear inside a NON-hit type (so we don't warn on them) — "Double
# Play" / "Triple Play" legitimately contain a hit word without being a hit.
_HIT_WORD_NONHIT = ("double play", "triple play")


def _is_hit_type(play_type: Optional[str]) -> bool:
    """True iff a play's `type` denotes a base hit. Matches by trimmed,
    lowercased SUFFIX against `_HIT_SUFFIXES`, so prefixed variants (Bunt/Infield
    Single, Ground Rule Double, Inside The Park Home Run) count while "Double
    Play"/"Triple Play" do not.

    Guard: if a type CONTAINS a hit word but does NOT match the suffix rule and
    isn't a known non-hit ("Double Play"/"Triple Play"), log a warning so any odd
    future BDL variant surfaces instead of being silently mis-handled."""
    t = (play_type or "").strip().lower()
    if not t:
        return False
    if any(t.endswith(sfx) for sfx in _HIT_SUFFIXES):
        return True
    # No suffix match — flag suspicious containers we didn't expect.
    if any(w in t for w in ("single", "double", "triple", "home run")) \
            and not any(w in t for w in _HIT_WORD_NONHIT):
        log.warning("play type %r contains a hit word but doesn't match the hit "
                    "suffix rule — not counted as a hit; verify BDL variant", play_type)
    return False


def _half_of(play: dict) -> Optional[tuple[int, str]]:
    """(inning, "top"|"bottom") for a real half-inning play, or None for the
    Start/Mid/End inning-transition markers (which carry no batting side)."""
    half = _norm_half(play.get("inning_type"))
    inning = play.get("inning")
    if half is None or inning is None:
        return None
    return (inning, half)


def _id_name_map(stats: list[dict]) -> dict[int, Optional[str]]:
    """player_id -> full_name, from /stats. Plays + PAs carry only ids, so this
    is the single join that lets the situation/plays surface player names."""
    names: dict[int, Optional[str]] = {}
    for s in stats:
        p = s.get("player") or {}
        if p.get("id") is not None:
            names[p["id"]] = p.get("full_name")
    return names


def _last_nonnull(plays_sorted: list[dict], key: str) -> Any:
    """Most-recent non-null value of `key` across the play feed — fills the
    current batter/pitcher when the freshest half-inning play is a marker row
    (e.g. "Start Inning") that carries no id. Stays PLAYS-only."""
    for p in reversed(plays_sorted):
        v = p.get(key)
        if v is not None:
            return v
    return None


def _derive_state(plays_sorted: list[dict]) -> dict:
    """Current live state from the play feed ALONE — no PA/games fallback (§2).

    `sit_play` = the latest play with a real Top/Bottom inning_type (skipping the
    Mid/End markers). A completed at-bat's rows are back-filled with the post-AB
    count/score, but the tail (in-progress) at-bat carries the CURRENT
    outs/count/score/matchup — exactly the live state we want.

    Runs totals are the MAX running score across all plays (§2c): running scores
    are monotonic, so max == the latest real score and is immune to a literal 0
    that an in-progress row might momentarily carry."""
    sit_play = next(
        (p for p in reversed(plays_sorted) if _half_of(p) is not None),
        None,
    )
    sp = sit_play or {}

    batter_id = sp.get("batter_id")
    if batter_id is None:
        batter_id = _last_nonnull(plays_sorted, "batter_id")
    pitcher_id = sp.get("pitcher_id")
    if pitcher_id is None:
        pitcher_id = _last_nonnull(plays_sorted, "pitcher_id")

    away_runs = max(
        (p["away_score"] for p in plays_sorted if p.get("away_score") is not None),
        default=0,
    )
    home_runs = max(
        (p["home_score"] for p in plays_sorted if p.get("home_score") is not None),
        default=0,
    )

    return {
        "inning":     sp.get("inning"),
        "half":       _norm_half(sp.get("inning_type")),
        "outs":       sp.get("outs"),
        "balls":      sp.get("balls"),
        "strikes":    sp.get("strikes"),
        "batter_id":  batter_id,
        "pitcher_id": pitcher_id,
        "away_runs":  away_runs,
        "home_runs":  home_runs,
    }


def _derive_grid(plays_sorted: list[dict]) -> list[dict]:
    """Per-inning runs grid from the play feed's running scores (§2a).

    For each real half-inning, runs = (running score at that half's LAST play)
    minus (running score at the previous half's last play), credited to the
    batting side (away bats top, home bats bottom).

    A cell is None IFF no play exists for that (inning, half):
      * None -> the half was NEVER batted (renders x/blank client-side)
      * 0    -> the half WAS played and scored no runs
    Emitted as JSON null, never the string "x": the client's LiveInningRow is
    Int?, so a string would break decoding. n_inn = max(9, max observed inning),
    so extra innings extend the grid automatically."""
    order: list[tuple[int, str]] = []          # (inning, half), chronological
    last_score: dict[tuple[int, str], tuple[Optional[int], Optional[int]]] = {}
    for p in plays_sorted:
        hk = _half_of(p)
        if hk is None:
            continue
        if hk not in last_score:
            order.append(hk)
        prev = last_score.get(hk)
        a = p.get("away_score")
        h = p.get("home_score")
        last_score[hk] = (
            a if a is not None else (prev[0] if prev else None),
            h if h is not None else (prev[1] if prev else None),
        )

    away_by_inning: dict[int, int] = {}
    home_by_inning: dict[int, int] = {}
    played: set[tuple[int, str]] = set()
    prev_a = prev_h = 0
    for hk in order:
        inning, half = hk
        la, lh = last_score[hk]
        la = la if la is not None else prev_a
        lh = lh if lh is not None else prev_h
        played.add(hk)
        if half == "top":
            away_by_inning[inning] = away_by_inning.get(inning, 0) + (la - prev_a)
        else:
            home_by_inning[inning] = home_by_inning.get(inning, 0) + (lh - prev_h)
        prev_a, prev_h = la, lh

    n_inn = max(9, max((i for (i, _h) in played), default=0))
    return [
        {
            "num":  i,
            "away": away_by_inning.get(i, 0) if (i, "top") in played else None,
            "home": home_by_inning.get(i, 0) if (i, "bottom") in played else None,
        }
        for i in range(1, n_inn + 1)
    ]


def _derive_hits(plays_sorted: list[dict]) -> tuple[int, int]:
    """(away_hits, home_hits) — count hit-outcome plays per batting half (§2b).
    Hit outcomes matched by suffix via `_is_hit_type` so prefixed variants
    (Bunt/Infield Single, Ground Rule Double, …) are included."""
    away = home = 0
    for p in plays_sorted:
        if _is_hit_type(p.get("type")):
            hk = _half_of(p)
            if hk is None:
                continue
            if hk[1] == "top":
                away += 1
            else:
                home += 1
    return away, home


def _derive_scoring(plays_sorted: list[dict]) -> list[dict]:
    """Full-game scoring-play subset, as play blocks (PLAYS)."""
    return [_play_block(p) for p in plays_sorted if p.get("scoring_play")]


def _bases_from_pas(pas: list[dict], inning: Optional[int],
                    half: Optional[str]) -> tuple[bool, bool, bool]:
    """Base-runner state from /plate_appearances — the ONLY feed with a
    structured base field (plays carry none). The PA feed lags, so its runners
    belong to whatever (older) half-inning its newest entry is in: trust them
    ONLY when that PA is the SAME inning + half as the current play, else show
    empty bases rather than bleeding stale runners into the situation. (Behavior
    unchanged from the pre-refactor inline logic.)"""
    pas_sorted = sorted(pas, key=lambda x: x.get("pa_number") or 0)
    lpa = pas_sorted[-1] if pas_sorted else {}
    matches = bool(
        lpa
        and lpa.get("inning") == inning
        and (lpa.get("half_inning") or "").lower() == (half or "")
    )
    return (
        bool(lpa.get("runner_on_first"))  if matches else False,
        bool(lpa.get("runner_on_second")) if matches else False,
        bool(lpa.get("runner_on_third"))  if matches else False,
    )


def _box_lines(stats: list[dict], home_team: dict, away_team: dict,
               lineup: Optional[list[dict]],
               plays_sorted: list[dict]) -> tuple[dict, dict]:
    """Per-player batting + pitching lines from /stats, sorted for display.
    Batting order joins /lineups (stats carries none); pitching order uses each
    pitcher's first appearance in the play feed (no extra BDL call). Behavior is
    unchanged from the pre-refactor inline assembly."""
    batting_order: dict[int, int] = {}
    for row in (lineup or []):
        pid = (row.get("player") or {}).get("id")
        slot = row.get("batting_order")
        if pid is not None and slot is not None:
            batting_order[pid] = slot

    pitch_appearance: dict[int, int] = {}
    for idx, pl in enumerate(plays_sorted):
        ppid = pl.get("pitcher_id")
        if ppid is not None and ppid not in pitch_appearance:
            pitch_appearance[ppid] = idx

    batting: dict[str, list] = {"away": [], "home": []}
    pitching: dict[str, list] = {"away": [], "home": []}
    for s in stats:
        side = _side_for_stat(s, home_team, away_team)
        if side is None:
            continue
        b = _bat_row(s)
        if b:
            b["batting_order"] = batting_order.get(b.get("id"))
            batting[side].append(b)
        p = _pit_row(s)
        if p:
            pitching[side].append(p)

    # Python's sort is stable, so:
    #   - batters: starters by slot 1-9; substitutes (no slot) fall after,
    #     keeping their /stats order.
    #   - pitchers: by play-feed appearance; any not found sort last.
    _LAST = float("inf")
    for side in ("away", "home"):
        batting[side].sort(
            key=lambda r: (r.get("batting_order") is None, r.get("batting_order") or 0)
        )
        pitching[side].sort(
            key=lambda r: pitch_appearance.get(r.get("id"), _LAST)
        )
    return batting, pitching


def assemble_unified(game: dict, stats: list[dict],
                     plays: list[dict], pas: list[dict],
                     lineup: Optional[list[dict]] = None) -> dict:
    """Thin orchestrator (§4): derive live state from PLAYS, then attach
    names/bases/lines/errors from their own feeds — ONE source per field.

      * live state + runs + linescore grid + hits  <- PLAYS  (_derive_*)
      * team identity + errors                      <- games  (_team_block)
      * batter/pitcher NAMES + stat lines           <- stats  (_id_name_map/_box_lines)
      * base runners                                <- PAs    (_bases_from_pas)

    Everything is recomputed from scratch from the current play list on every
    call — no stateful accumulators — so if balldontlie revises a recent play's
    running score between polls, the derived grid/hits/state self-correct."""
    home_team = game.get("home_team") or {}
    away_team = game.get("away_team") or {}
    home_data = game.get("home_team_data") or {}
    away_data = game.get("away_team_data") or {}
    status = _norm_status(game.get("status"))

    plays_sorted = sorted(plays, key=lambda p: p.get("order") or 0)

    # PLAYS — the single source of truth for live state.
    state = _derive_state(plays_sorted)
    grid = _derive_grid(plays_sorted)
    away_hits, home_hits = _derive_hits(plays_sorted)
    scoring = _derive_scoring(plays_sorted)
    full_plays = [_play_block(p) for p in plays_sorted]
    away_runs = state["away_runs"]
    home_runs = state["home_runs"]

    # Other feeds — attached, one source each.
    names = _id_name_map(stats)                                 # stats: id -> name
    on_first, on_second, on_third = _bases_from_pas(            # PAs (inning/half-gated)
        pas, state["inning"], state["half"],
    )
    batting, pitching = _box_lines(                            # stats (+lineup/plays order)
        stats, home_team, away_team, lineup, plays_sorted,
    )
    batter_id = state["batter_id"]
    pitcher_id = state["pitcher_id"]

    # Header/summary team blocks: identity + errors from games (_team_block);
    # runs + hits overridden with the PLAYS-derived values so every surface
    # (list card, header banner, linescore) reads the same numbers.
    away_summary = _team_block(away_team, away_data)
    home_summary = _team_block(home_team, home_data)
    away_summary["runs"] = away_runs
    home_summary["runs"] = home_runs
    away_summary["hits"] = away_hits
    home_summary["hits"] = home_hits

    return {
        "game_id":     game.get("id"),
        "fetched_at":  _now_iso(),
        "status":      status,
        "season":      game.get("season"),
        "season_type": game.get("season_type"),
        "summary": {
            "away":        away_summary,
            "home":        home_summary,
            "inning":      state["inning"],
            "inning_half": state["half"],
            "outs":        state["outs"],
            "balls":       state["balls"],
            "strikes":     state["strikes"],
            "is_live":     status == "in_progress",
        },
        "linescore": {
            "innings":           grid,
            "away_runs":         away_runs,
            "home_runs":         home_runs,
            "away_hits":         away_hits,
            "home_hits":         home_hits,
            "away_errors":       away_data.get("errors"),
            "home_errors":       home_data.get("errors"),
            "scheduled_innings": 9,
        },
        "situation": {
            "batter":   {"id": batter_id,  "name": names.get(batter_id)}  if batter_id  else None,
            "pitcher":  {"id": pitcher_id, "name": names.get(pitcher_id)} if pitcher_id else None,
            "on_first":  on_first,
            "on_second": on_second,
            "on_third":  on_third,
        },
        "plays":         full_plays,
        "scoring_plays": scoring,
        "batting":       batting,
        "pitching":      pitching,
    }


def _summary_from_unified(u: dict) -> dict:
    """Compact card for the /live/games list (Scores list / Home).

    Enriched (Phase 1 §3) with the current batter/pitcher (id + name) and the
    latest play text — all pulled from the already-assembled `unified` dict, so
    this adds ZERO balldontlie calls. The three new fields are ADDITIVE: the iOS
    client ignores unknown keys, so the payload contract is preserved (§5)."""
    s = u["summary"]
    sit = u.get("situation") or {}
    plays = u.get("plays") or []
    last_play_text = plays[-1].get("text") if plays else None
    return {
        "game_id":     u["game_id"],
        "fetched_at":  u["fetched_at"],
        "status":      u["status"],
        "is_live":     s["is_live"],
        "inning":      s["inning"],
        "inning_half": s["inning_half"],
        "outs":        s["outs"],
        "away": {
            "team_code":    s["away"]["team_code"],
            "abbreviation": s["away"]["abbreviation"],
            "name":         s["away"]["name"],
            "runs":         s["away"]["runs"],
        },
        "home": {
            "team_code":    s["home"]["team_code"],
            "abbreviation": s["home"]["abbreviation"],
            "name":         s["home"]["name"],
            "runs":         s["home"]["runs"],
        },
        # Additive enrichment (Phase 1) — batter/pitcher {id, name} and the last
        # play text, straight from the cached snapshot (no extra BDL call).
        "batter":    sit.get("batter"),
        "pitcher":   sit.get("pitcher"),
        "last_play": last_play_text,
    }


# --- Background loop -------------------------------------------------------

_loop_task: Optional[asyncio.Task] = None
# -inf so the very first cycle always fetches the slate (the asyncio monotonic
# clock starts near 0 at boot, which would otherwise delay the first fetch).
_last_schedule_check: float = float("-inf")


async def _refresh_cycle() -> int:
    """One refresh pass. Returns the number of balldontlie calls made this
    cycle (for quota logging)."""
    global _last_schedule_check
    loop_now = asyncio.get_event_loop().time()
    have_live = any(k for k in [_cache.get(_SUMMARY_KEY)] if k and k.get("count"))

    bdl_calls = 0
    slate = None

    # Pull the slate every cycle while games are live (keeps the score grid +
    # the live set fresh and detects finals immediately); when idle, only every
    # SCHEDULE_INTERVAL_S — so zero per-game polling AND a minimal heartbeat
    # when nothing is live.
    if have_live or (loop_now - _last_schedule_check) >= SCHEDULE_INTERVAL_S:
        slate = await _fetch_slate()
        bdl_calls += 1
        _last_schedule_check = loop_now

    if slate is None:
        return 0   # idle, not yet time to re-check the slate

    games_by_id = {g.get("id"): g for g in slate if g.get("id") is not None}
    live_ids = [gid for gid, g in games_by_id.items()
                if _norm_status(g.get("status")) == "in_progress"]

    if not live_ids:
        # Nothing live — publish an empty list and evict any stale snapshots.
        _cache.set(_SUMMARY_KEY, {"fetched_at": _now_iso(), "count": 0, "games": []},
                   LIVE_CACHE_TTL_S)
        return bdl_calls

    summaries = []
    for gid in live_ids:
        try:
            await asyncio.sleep(BDL_PACING_S)
            stats = await _fetch_stats(gid); bdl_calls += 1
            await asyncio.sleep(BDL_PACING_S)
            plays = await _fetch_plays(gid); bdl_calls += 1
            await asyncio.sleep(BDL_PACING_S)
            pas = await _fetch_pas(gid); bdl_calls += 1
            # Lineup drives batting order; cached per game, so this makes a BDL
            # call only on the first cycle we see the game (and any retry after a
            # failed/empty fetch) — steady-state cost is 0.
            lineup, lineup_called = await _get_lineup_cached(gid)
            if lineup_called:
                bdl_calls += 1

            unified = assemble_unified(games_by_id[gid], stats, plays, pas, lineup)
            _cache.set(_game_key(gid), unified, LIVE_CACHE_TTL_S)
            summaries.append(_summary_from_unified(unified))
        except Exception:
            log.exception("live refresh failed for game %s — skipping this cycle", gid)

    _cache.set(_SUMMARY_KEY,
               {"fetched_at": _now_iso(), "count": len(summaries), "games": summaries},
               LIVE_CACHE_TTL_S)
    return bdl_calls


async def _live_loop() -> None:
    log.info("live loop started (refresh %.0fs, schedule heartbeat %.0fs)",
             REFRESH_INTERVAL_S, SCHEDULE_INTERVAL_S)
    while True:
        started = asyncio.get_event_loop().time()
        try:
            calls = await _refresh_cycle()
            summary = _cache.get(_SUMMARY_KEY) or {}
            n_live = summary.get("count", 0)
            if calls:
                log.info("live cycle: %d live game(s), %d balldontlie call(s)",
                         n_live, calls)
        except Exception:
            log.exception("live cycle crashed — continuing")
        # Adaptive sleep: keep ~REFRESH_INTERVAL_S between cycle STARTS even
        # when the per-game fan-out (paced at %.2fs) took a while.
        elapsed = asyncio.get_event_loop().time() - started
        await asyncio.sleep(max(1.0, REFRESH_INTERVAL_S - elapsed))


def start_live_loop() -> None:
    """Launch the background loop once. Safe to call multiple times — only the
    first start within the process takes effect (guards the single-worker
    assumption against accidental double-start)."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        log.info("live loop already running — not starting again")
        return
    _loop_task = asyncio.create_task(_live_loop())


def stop_live_loop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


# --- Public getters (read cache only — never call balldontlie) ------------

def get_live_summary() -> dict:
    """All currently-live games as compact cards. Empty list when nothing is
    live (or before the first cycle has run)."""
    return _cache.get(_SUMMARY_KEY) or {"fetched_at": _now_iso(), "count": 0, "games": []}


def get_live_game(game_id: int) -> Optional[dict]:
    """The full unified snapshot for one live game, or None if it isn't
    currently live / not in cache."""
    return _cache.get(_game_key(game_id))
