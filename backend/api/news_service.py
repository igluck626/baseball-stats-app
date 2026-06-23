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


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

_UPSERT_SQL = text("""
    INSERT INTO news_articles
        (source, source_name, team_code, title, summary, url, image_url, published_at, fetched_at)
    VALUES
        (:source, :source_name, :team_code, :title, :summary, :url, :image_url, :published_at, now())
    ON CONFLICT (url) DO UPDATE SET
        title      = EXCLUDED.title,
        summary    = EXCLUDED.summary,
        image_url  = EXCLUDED.image_url,
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
                row = db.execute(_UPSERT_SQL, {
                    "source":       _SOURCE,
                    "source_name":  _SOURCE_NAME,
                    "team_code":    team_code,
                    "title":        title,
                    "summary":      _strip_html(entry.get("summary")),
                    "url":          url,
                    "image_url":    _image_for(entry, image_by_link),
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
        "SELECT id, source_name, team_code, title, summary, url, image_url, published_at "
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

    return [
        {
            "id":           r["id"],
            "source_name":  r["source_name"],
            "team_code":    r["team_code"],
            "title":        r["title"],
            "summary":      r["summary"],
            "url":          r["url"],
            "image_url":    r["image_url"],
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        }
        for r in rows
    ]
