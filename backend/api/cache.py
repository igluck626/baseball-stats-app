"""In-process TTL cache for the FastAPI backend.

Wraps the hottest DB-backed read endpoints so popular players
(Trout, Ohtani, Judge — opened hundreds of times an hour) and
standings/leaderboards don't burn a Postgres SELECT per request
when the data hasn't changed since the nightly run.

Cache invalidation:
  • `cache.clear()` is called at the end of every nightly run,
    so iOS sees the new numbers immediately after the cron.
  • `POST /admin/cache/clear` is available for ad-hoc evicts.
  • Entries naturally expire via TTL.

Scope caveat: Railway can run multiple workers (currently one,
but the deployment can scale horizontally). Each worker keeps
its own cache — this is acceptable while we're single-worker.
When multi-worker is needed, swap the dict for a shared Redis
client (the public API stays the same).
"""

import threading
import time
from typing import Any, Optional


class TTLCache:
    """Simple thread-safe in-memory TTL cache. Reset on server
    restart. Values are stored as-is — no serialization, no size
    cap (the working set is small enough that we don't need LRU
    eviction yet; can add later if memory becomes a concern)."""

    def __init__(self):
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value when it exists and hasn't
        expired, else None. Expired entries are evicted on read."""
        with self._lock:
            if key in self._store:
                value, expires_at = self._store[key]
                if time.time() < expires_at:
                    return value
                del self._store[key]
        return None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            self._store[key] = (value, time.time() + ttl_seconds)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        """Cache health snapshot for the /admin/cache/stats endpoint."""
        with self._lock:
            now = time.time()
            total = len(self._store)
            active = sum(1 for _, exp in self._store.values() if exp > now)
            return {
                "total_keys":   total,
                "active_keys":  active,
                "expired_keys": total - active,
            }


# Module-level singleton — same instance shared across every
# request thread in the worker. Reset on restart; cleared at
# nightly-update end via `cache.clear()`.
cache = TTLCache()
