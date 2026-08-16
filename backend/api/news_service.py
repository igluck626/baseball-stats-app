"""Team news ingestion from MLB.com's official per-team RSS feeds.

Self-contained Phase-1 feature: a `news_articles` table, an ingester that
walks all 30 teams' feeds and upserts items (deduped on URL), and read
helpers for the `/news` endpoint. Not wired into the nightly pipeline — the
manual `/admin/refresh-news` endpoint drives it for now.

Team identity: `team_code` is the app's canonical Lahman team code (the same
`team_id` used in `team_seasons` and the leaderboard/team-leaders filters,
e.g. CHN=Cubs, LAN=Dodgers, NYA=Yankees), so the rest of the app can query
news with the codes it already has.

MLB.com feed quirks (verified against the live feeds):
  • Per-team URL: https://www.mlb.com/{slug}/feeds/news/rss.xml
  • Items carry <title>, <link>, <pubDate> (RFC-822), and a NON-standard
    <image href="..."/> element. They usually have NO <description>, so
    `summary` is typically null. feedparser does not pick up <image href>,
    so the image is pulled from the raw XML by link.
"""
import calendar
import datetime
import logging
import re
import threading
import time
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup
from sqlalchemy import text

from database import connection

log = logging.getLogger("news")

# Canonical app team code (Lahman) → MLB.com URL slug. All 30 validated to
# return HTTP 200 + valid RSS with items.
_TEAM_SLUGS: dict[str, str] = {
    "ARI": "dbacks",   "ATL": "braves",    "BAL": "orioles",
    "BOS": "redsox",   "CHA": "whitesox",  "CHN": "cubs",
    "CIN": "reds",     "CLE": "guardians", "COL": "rockies",
    "DET": "tigers",   "HOU": "astros",    "KCA": "royals",
    "LAA": "angels",   "LAN": "dodgers",   "MIA": "marlins",
    "MIL": "brewers",  "MIN": "twins",     "NYA": "yankees",
    "NYN": "mets",     "OAK": "athletics", "PHI": "phillies",
    "PIT": "pirates",  "SDN": "padres",    "SFN": "giants",
    "SEA": "mariners", "SLN": "cardinals", "TBA": "rays",
    "TEX": "rangers",  "TOR": "bluejays",  "WAS": "nationals",
}

_SOURCE      = "mlb"
_SOURCE_NAME = "MLB.com"
_FEED_URL    = "https://www.mlb.com/{slug}/feeds/news/rss.xml"
_USER_AGENT  = "BaseballStatsApp/1.0 (+https://baseball-stats-app)"
_FETCH_TIMEOUT_SECONDS = 25
_POLITE_DELAY_SECONDS  = 0.3
_RETENTION_DAYS        = 30
_SUMMARY_MAX_CHARS     = 600

# Match each <item> block, its <link>, and the non-standard <image href>.
_ITEM_RE  = re.compile(r"<item>(.*?)</item>", re.S)
_LINK_RE  = re.compile(r"<link>(.*?)</link>", re.S)
_IMAGE_RE = re.compile(r'<image\b[^>]*\bhref="([^"]+)"')

# ---------------------------------------------------------------------------
# Article images — IN MEMORY ONLY, never persisted
# ---------------------------------------------------------------------------
# Images were removed entirely on 2026-08-15 and restored on 2026-08-16 under a
# deliberately different posture: the URL is harvested from the feed, held in
# this process, attached to the response, and never written to Postgres. The
# `news_articles.image_url` COLUMN still exists and is now neither read nor
# written by any code path — it is inert and droppable once the old-code deploy
# window has closed.
#
# The map is filled by the 20-minute crawl that already walks all 30 feeds, so
# it costs no extra request to MLB and no request latency — we simply stop
# discarding a value the crawl already parses.
#
# ⚠️ SINGLE-PROCESS ASSUMPTION. This is per-process state. The Procfile runs one
# bare `uvicorn main:app` (no --workers) and railway.toml sets no replica count,
# so today there is exactly one process and the crawl thread lives inside it.
# If the app is ever scaled to multiple replicas or uvicorn workers, each gets
# its OWN map: a given article would show an image on one replica and not on
# another, depending on which process served the request and whether its crawl
# had run. Fixing that means either a shared cache (Redis) or accepting the
# inconsistency — it does NOT mean going back to the database column.
_IMAGE_BY_URL: dict[str, str] = {}
_image_lock = threading.Lock()
# Team codes with a lazy fill in flight, and the last time each was attempted —
# so a cold map can't spawn the same fetch repeatedly under concurrent requests.
_image_fill_inflight: set[str] = set()
_image_fill_attempted: dict[str, float] = {}
_IMAGE_FILL_COOLDOWN_SECONDS = 300


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS news_articles (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    team_code    TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT,
    url          TEXT NOT NULL,
    -- RETAINED, NEVER WRITTEN, NEVER READ (since 2026-08-15). Article images
    -- were removed; the column stays because dropping it would open a window
    -- during a rolling deploy where the previous revision still INSERTs into
    -- it. Existing values are nulled by a one-off backfill run AFTER this code
    -- deploys — running it before would let the old code repopulate them on
    -- the next poll.
    image_url    TEXT,
    published_at TIMESTAMPTZ,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
# Unique index on url is the ON CONFLICT dedupe target; composite index
# powers the team-scoped newest-first query.
_CREATE_URL_INDEX_SQL  = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_news_articles_url "
    "ON news_articles (url)"
)
_CREATE_TEAM_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_news_team_published "
    "ON news_articles (team_code, published_at DESC)"
)


def ensure_news_table() -> None:
    """Create the table + indexes if missing. Idempotent — safe to call on
    every request / run (CREATE ... IF NOT EXISTS is a no-op once present)."""
    with connection.get_session() as db:
        db.execute(text(_CREATE_TABLE_SQL))
        db.execute(text(_CREATE_URL_INDEX_SQL))
        db.execute(text(_CREATE_TEAM_INDEX_SQL))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _strip_html(value: Optional[str]) -> Optional[str]:
    """Plain text from an HTML/CDATA summary, whitespace-collapsed and
    trimmed. Returns None for empty/missing input."""
    if not value:
        return None
    txt = BeautifulSoup(value, "html.parser").get_text(separator=" ")
    txt = re.sub(r"\s+", " ", txt).strip()
    if not txt:
        return None
    return txt[:_SUMMARY_MAX_CHARS].rstrip()


def _published_to_utc(entry) -> Optional[datetime.datetime]:
    """Parse the item's publish date to a timezone-aware UTC datetime.
    feedparser normalizes `published_parsed` to a UTC struct_time."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st is None:
        return None
    try:
        epoch = calendar.timegm(st)   # struct_time is UTC → epoch
        return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    except (ValueError, OverflowError):
        return None


def _image_for(entry, image_by_link: dict[str, str]) -> Optional[str]:
    """Image URL via the standard chain (media:content → media:thumbnail →
    enclosure), then feedparser's <image>, then MLB.com's non-standard
    item-level <image href> (looked up by link from the raw feed). None when
    nothing is found — never fabricated."""
    media = entry.get("media_content") or []
    for m in media:
        if m.get("url"):
            return m["url"]
    thumbs = entry.get("media_thumbnail") or []
    for t in thumbs:
        if t.get("url"):
            return t["url"]
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and (link.get("type") or "").startswith("image"):
            if link.get("href"):
                return link["href"]
    img = entry.get("image")
    if isinstance(img, dict) and img.get("href"):
        return img["href"]
    # MLB.com per-item <image href> (feedparser ignores it).
    return image_by_link.get((entry.get("link") or "").strip())


def _image_map_from_raw(raw: str) -> dict[str, str]:
    """{link -> image href} built from the raw feed XML, for the MLB.com
    <image href> element feedparser drops."""
    out: dict[str, str] = {}
    for block in _ITEM_RE.findall(raw):
        link_m = _LINK_RE.search(block)
        img_m  = _IMAGE_RE.search(block)
        if link_m and img_m:
            out[link_m.group(1).strip()] = img_m.group(1).strip()
    return out


def _harvest_images(team_code: str) -> dict[str, str]:
    """Fetch one team's feed and return {article_url -> image_url}. Used by both
    the crawl and the lazy fill. Raises on HTTP / parse failure."""
    entries, image_by_link = _fetch_feed(team_code)
    found: dict[str, str] = {}
    for entry in entries:
        url = (entry.get("link") or "").strip()
        if not url:
            continue
        img = _image_for(entry, image_by_link)
        if img:
            found[url] = img
    return found


def _fill_images_worker(codes: list[str]) -> None:
    """Walk `codes` sequentially — one thread, `_POLITE_DELAY_SECONDS` apart, so
    a cold map can never turn into 30 simultaneous requests at MLB."""
    for code in codes:
        try:
            found = _harvest_images(code)
            with _image_lock:
                _IMAGE_BY_URL.update(found)
            log.info("news: lazy image fill %s -> %d", code, len(found))
        except Exception as exc:   # noqa: BLE001 — cosmetic data, never raise
            log.warning("news: lazy image fill failed for %s: %s", code, exc)
        finally:
            with _image_lock:
                _image_fill_inflight.discard(code)
        time.sleep(_POLITE_DELAY_SECONDS)


def _ensure_images(team_codes) -> None:
    """Kick a background fill for any of `team_codes` whose images we don't have.

    Covers the cold-start window: the map lives in memory, so after a deploy it
    is empty until the next 20-minute crawl. Rather than serve twenty minutes of
    image-less news, the first request for a team warms that team's feed. The
    cooldown stops a team whose feed genuinely carries no images from being
    re-fetched on every request.
    """
    now = time.monotonic()
    todo: list[str] = []
    with _image_lock:
        for code in team_codes:
            if code not in _TEAM_SLUGS or code in _image_fill_inflight:
                continue
            last = _image_fill_attempted.get(code)
            if last is not None and (now - last) < _IMAGE_FILL_COOLDOWN_SECONDS:
                continue
            _image_fill_attempted[code] = now
            _image_fill_inflight.add(code)
            todo.append(code)
    if todo:
        threading.Thread(target=_fill_images_worker, args=(todo,),
                         daemon=True, name="news-image-fill").start()


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

# `image_url` is deliberately absent from both the column list and the DO UPDATE
# set. The COLUMN still exists (see _CREATE_TABLE_SQL) — dropping it would open a
# window during a rolling deploy where the previous revision still writes to a
# missing column. Omitting it here means new rows get NULL and, crucially, that
# re-ingesting an existing article no longer refreshes a stored image URL.
_UPSERT_SQL = text("""
    INSERT INTO news_articles
        (source, source_name, team_code, title, summary, url, published_at, fetched_at)
    VALUES
        (:source, :source_name, :team_code, :title, :summary, :url, :published_at, now())
    ON CONFLICT (url) DO UPDATE SET
        title      = EXCLUDED.title,
        summary    = EXCLUDED.summary,
        fetched_at = now()
    RETURNING (xmax = 0) AS inserted
""")


def _fetch_feed(team_code: str) -> tuple[list, dict[str, str]]:
    """Fetch + parse one team's feed. Returns (feedparser entries, image map).
    Raises on HTTP / parse failure so the caller can record the feed as
    failed."""
    url = _FEED_URL.format(slug=_TEAM_SLUGS[team_code])
    resp = requests.get(
        url, timeout=_FETCH_TIMEOUT_SECONDS, headers={"User-Agent": _USER_AGENT}
    )
    resp.raise_for_status()
    raw = resp.text
    parsed = feedparser.parse(raw.encode("utf-8"))
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"feed did not parse: {parsed.get('bozo_exception')}")
    return parsed.entries, _image_map_from_raw(raw)


def refresh_news() -> dict:
    """Walk all 30 teams' MLB.com feeds and upsert items into news_articles
    (deduped on url). Each team is isolated in its own try/except so one bad
    feed doesn't abort the run. Prunes articles older than the retention
    window at the end. Returns a per-team + totals summary."""
    ensure_news_table()

    teams: dict[str, dict] = {}
    failed: list[dict] = []
    total_inserted = 0
    total_updated = 0

    # Article images for THIS walk. Built alongside the upserts and swapped in
    # at the end, which keeps the map bounded to what the feeds currently list
    # instead of growing forever as articles come and go.
    fresh_images: dict[str, str] = {}

    for team_code in _TEAM_SLUGS:
        try:
            entries, image_by_link = _fetch_feed(team_code)
        except Exception as exc:   # network / HTTP / parse — record + continue
            log.warning("news: feed failed for %s: %s", team_code, exc)
            failed.append({"team": team_code, "error": str(exc)})
            time.sleep(_POLITE_DELAY_SECONDS)
            continue

        inserted = 0
        updated = 0
        with connection.get_session() as db:
            for entry in entries:
                url = (entry.get("link") or "").strip()
                title = (entry.get("title") or "").strip()
                if not url or not title:
                    continue   # url is the dedupe key; title is required
                # In-memory only — deliberately NOT part of the upsert below.
                img = _image_for(entry, image_by_link)
                if img:
                    fresh_images[url] = img
                row = db.execute(_UPSERT_SQL, {
                    "source":       _SOURCE,
                    "source_name":  _SOURCE_NAME,
                    "team_code":    team_code,
                    "title":        title,
                    "summary":      _strip_html(entry.get("summary")),
                    "url":          url,
                    "published_at": _published_to_utc(entry),
                }).first()
                if row is not None and row.inserted:
                    inserted += 1
                else:
                    updated += 1

        teams[team_code] = {
            "fetched":  len(entries),
            "inserted": inserted,
            "updated":  updated,
        }
        total_inserted += inserted
        total_updated += updated
        time.sleep(_POLITE_DELAY_SECONDS)

    # Retention prune — drop anything past the window so the table stays bounded.
    pruned = 0
    try:
        with connection.get_session() as db:
            result = db.execute(text(
                "DELETE FROM news_articles "
                "WHERE published_at < now() - make_interval(days => :days)"
            ), {"days": _RETENTION_DAYS})
            pruned = result.rowcount or 0
    except Exception as exc:
        log.warning("news: retention prune failed: %s", exc)

    # Swap the image map in. On a clean walk this REPLACES the previous one, so
    # entries for articles that have dropped out of every feed go with it. If
    # any feed failed we merge instead, rather than blank that team's images on
    # the strength of one bad fetch.
    with _image_lock:
        if failed:
            _IMAGE_BY_URL.update(fresh_images)
        else:
            _IMAGE_BY_URL.clear()
            _IMAGE_BY_URL.update(fresh_images)
        _image_fill_attempted.clear()   # a full walk supersedes any lazy fill
        held = len(_IMAGE_BY_URL)
    log.info("news: image map now holds %d url(s), in memory only", held)

    return {
        "status":         "ok",
        "teams":          teams,
        "total_inserted": total_inserted,
        "total_updated":  total_updated,
        "failed_feeds":   failed,
        "pruned":         pruned,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_news(team: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Newest-first articles, optionally filtered to one team_code. `limit`
    is clamped to 1…50 by the caller. Returns the public article shape."""
    ensure_news_table()
    sql = (
        # image_url is intentionally NOT selected — the column survives for
        # deploy-ordering safety but is never read back out.
        "SELECT id, source_name, team_code, title, summary, url, published_at "
        "FROM news_articles "
    )
    params: dict = {"limit": limit}
    if team:
        sql += "WHERE team_code = :team "
        params["team"] = team
    # NULLS LAST so a missing publish date never floats to the top.
    sql += "ORDER BY published_at DESC NULLS LAST, id DESC LIMIT :limit"

    with connection.get_session() as db:
        rows = db.execute(text(sql), params).mappings().all()

    # Attach the image from memory. `.get` means an article the feed no longer
    # lists simply has none — which is the intended behaviour, not a failure.
    with _image_lock:
        images = {r["url"]: _IMAGE_BY_URL.get(r["url"]) for r in rows}

    out = [
        {
            "id":           r["id"],
            "source_name":  r["source_name"],
            "team_code":    r["team_code"],
            "title":        r["title"],
            "summary":      r["summary"],
            "url":          r["url"],
            "image_url":    images.get(r["url"]),
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        }
        for r in rows
    ]

    # Cold-map warm-up. Only for teams that returned rows with no image at all —
    # a warm map costs nothing here, and the fill is cooldown-guarded and runs
    # on one sequential background thread.
    missing = {row["team_code"] for row in out
               if row["image_url"] is None and row["team_code"]}
    if missing:
        _ensure_images(missing)

    return out
