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


def assemble_unified(game: dict, stats: list[dict],
                     plays: list[dict], pas: list[dict],
                     lineup: Optional[list[dict]] = None) -> dict:
    """Build the single consistent per-game snapshot from this cycle's four
    balldontlie payloads. Everything a client renders comes from here, so the
    score, plays, and inning/base state can never skew relative to each other.

    `lineup` (from /lineups, cached per game) supplies batting order — /stats
    itself carries none and ships teams interleaved. When absent (fetch failed /
    not posted yet) batters fall back to /stats order for this cycle."""
    home_team = game.get("home_team") or {}
    away_team = game.get("away_team") or {}
    home_data = game.get("home_team_data") or {}
    away_data = game.get("away_team_data") or {}
    status = _norm_status(game.get("status"))

    # Player id -> name, so the situation/plays can show names (plays + PAs
    # only carry ids).
    names: dict[int, Optional[str]] = {}
    for s in stats:
        p = s.get("player") or {}
        if p.get("id") is not None:
            names[p["id"]] = p.get("full_name")

    plays_sorted = sorted(plays, key=lambda p: p.get("order") or 0)
    last_play = plays_sorted[-1] if plays_sorted else None
    pas_sorted = sorted(pas, key=lambda x: x.get("pa_number") or 0)
    last_pa = pas_sorted[-1] if pas_sorted else None

    # Score: authoritative team totals from /games; fall back to the running
    # score on the last play.
    away_runs = away_data.get("runs")
    home_runs = home_data.get("runs")
    if away_runs is None and last_play:
        away_runs = last_play.get("away_score")
    if home_runs is None and last_play:
        home_runs = last_play.get("home_score")

    # Current inning / half / count: last PA leads (base state), last play fills
    # count (balls/strikes live only on plays).
    lp = last_play or {}
    lpa = last_pa or {}
    inning = lpa.get("inning") or lp.get("inning") or game.get("period")
    half = (lpa.get("half_inning") or lp.get("inning_type") or "").lower() or None
    outs = lpa.get("outs")
    if outs is None:
        outs = lp.get("outs")
    balls = lp.get("balls")
    strikes = lp.get("strikes")
    batter_id = lpa.get("batter_id") or lp.get("batter_id")
    pitcher_id = lpa.get("pitcher_id") or lp.get("pitcher_id")

    # Linescore grid.
    away_inn = away_data.get("inning_scores") or []
    home_inn = home_data.get("inning_scores") or []
    n_inn = max(len(away_inn), len(home_inn), 9)
    innings = [
        {
            "num":  i + 1,
            "away": away_inn[i] if i < len(away_inn) else None,
            "home": home_inn[i] if i < len(home_inn) else None,
        }
        for i in range(n_inn)
    ]

    # Full game (now that _fetch_plays paginates) — the client's "All plays" view
    # needs the whole feed, not just a tail window. scoring_plays stays the
    # full-game scoring subset.
    full_plays = [_play_block(p) for p in plays_sorted]
    scoring = [_play_block(p) for p in plays_sorted if p.get("scoring_play")]

    # Batting order: player_id -> lineup slot (1-9) from /lineups. /stats has no
    # order field, so without this join batters render in balldontlie's arbitrary
    # (team-interleaved) order.
    batting_order: dict[int, int] = {}
    for row in (lineup or []):
        pid = (row.get("player") or {}).get("id")
        slot = row.get("batting_order")
        if pid is not None and slot is not None:
            batting_order[pid] = slot

    # Pitching order: first appearance of each pitcher_id in the play feed we
    # already fetched (starter first, then relievers as they entered) — no extra
    # BDL call. Pitchers absent from plays sort to the end.
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

    # Sort each side. Python's sort is stable, so:
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

    return {
        "game_id":     game.get("id"),
        "fetched_at":  _now_iso(),
        "status":      status,
        "season":      game.get("season"),
        "season_type": game.get("season_type"),
        "summary": {
            "away":        _team_block(away_team, away_data),
            "home":        _team_block(home_team, home_data),
            "inning":      inning,
            "inning_half": half,
            "outs":        outs,
            "balls":       balls,
            "strikes":     strikes,
            "is_live":     status == "in_progress",
        },
        "linescore": {
            "innings":           innings,
            "away_runs":         away_runs,
            "home_runs":         home_runs,
            "away_hits":         away_data.get("hits"),
            "home_hits":         home_data.get("hits"),
            "away_errors":       away_data.get("errors"),
            "home_errors":       home_data.get("errors"),
            "scheduled_innings": 9,
        },
        "situation": {
            "batter":   {"id": batter_id,  "name": names.get(batter_id)}  if batter_id  else None,
            "pitcher":  {"id": pitcher_id, "name": names.get(pitcher_id)} if pitcher_id else None,
            "on_first":  bool(lpa.get("runner_on_first")),
            "on_second": bool(lpa.get("runner_on_second")),
            "on_third":  bool(lpa.get("runner_on_third")),
        },
        "plays":         full_plays,
        "scoring_plays": scoring,
        "batting":       batting,
        "pitching":      pitching,
    }


def _summary_from_unified(u: dict) -> dict:
    """Compact card for the /live/games list (Scores list / Home)."""
    s = u["summary"]
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
