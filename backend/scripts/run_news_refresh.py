#!/usr/bin/env python3
"""Trigger the team-news refresh via HTTP. Designed to run as a Railway cron service.

Reads the API base URL from the BASEBALL_API_URL env var (e.g.
"https://baseball.up.railway.app"), POSTs to /admin/refresh-news, prints the
response, and exits 0 on success / 1 on failure.

Usage:
    BASEBALL_API_URL=https://your-domain backend/venv/bin/python backend/scripts/run_news_refresh.py

The endpoint kicks the refresh off in a background thread and returns
immediately ({"status": "started"} or "already_running"), so this script's
HTTP call completes in well under a second. Watch the app logs afterward for
the per-team + totals summary of that run.
"""

import os
import sys

import requests


def _admin_headers() -> dict:
    """The admin token, read from the SAME environment this script runs in.

    ⚠️ EXITS RATHER THAN CALLING WITHOUT IT. The endpoint answers 404 to an
    unauthenticated caller, so a missing token would surface as "route not
    found" — which reads like a deploy problem and would send the next person
    looking in the wrong place entirely.
    """
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        print("ERROR: ADMIN_TOKEN is not set", file=sys.stderr)
        raise SystemExit(1)
    return {"X-Admin-Token": token}


def main() -> int:
    api_url = os.getenv("BASEBALL_API_URL", "").strip().rstrip("/")
    if not api_url:
        print("ERROR: BASEBALL_API_URL is not set", file=sys.stderr)
        return 1

    endpoint = f"{api_url}/admin/refresh-news"
    print(f"POST {endpoint}")

    try:
        r = requests.post(endpoint, headers=_admin_headers(), timeout=30)
    except requests.RequestException as exc:
        print(f"ERROR: request failed: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {r.status_code}")
    print(r.text)

    if not r.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
